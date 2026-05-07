import unittest

from app.services.artifact_edit_plan import ArtifactEditPlanner
from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.tools.canvas_tool import CanvasTool
from app.services.presentation_artifact_service import PresentationArtifactService
from app.services.tools.presentation_tool import PresentationTool


class ArtifactEditPlanTests(unittest.TestCase):
    def test_planner_prefers_structured_llm_edit_plan(self) -> None:
        plan = ArtifactEditPlanner.from_llm_result(
            {
                "artifact_edit_plan": {
                    "artifact_type": "slides",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "delete",
                            "target": {"kind": "slide", "queries": ["P2"]},
                            "reason": "remove redundant page",
                        }
                    ],
                }
            },
            artifact_type="slides",
            instruction="删掉第二页",
            available_targets=["P1", "P2"],
        )

        self.assertTrue(plan.mutation_required)
        self.assertEqual(plan.operations[0].op_type, "delete")
        self.assertEqual(plan.operations[0].target["queries"], ["P2"])

    def test_planner_fallback_resolves_targets_without_artifact_specific_rules(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="帮我删除 Update (2026-04-29 15:43) 栏一下的内容",
            available_targets=["讨论摘要", "Update (2026-04-29 15:43)"],
        )

        self.assertTrue(plan.mutation_required)
        self.assertEqual(plan.operations[0].op_type, "delete")
        self.assertEqual(plan.operations[0].target["queries"], ["Update (2026-04-29 15:43)"])

    def test_planner_fallback_keeps_multiple_edit_intents(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="删除 Update (2026-04-29 15:43) 栏，然后重新总结一下文档",
            available_targets=["讨论摘要", "Update (2026-04-29 15:43)"],
        )

        self.assertTrue(plan.mutation_required)
        self.assertEqual([operation.op_type for operation in plan.operations], ["delete", "rewrite"])
        self.assertEqual(plan.operations[0].target["queries"], ["Update (2026-04-29 15:43)"])

    def test_planner_fallback_keeps_independent_targets_for_many_ops(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="删除旧风险；补充验收标准；把下一步建议挪到前面",
            available_targets=["旧风险", "验收标准", "下一步建议"],
        )

        self.assertEqual([operation.op_type for operation in plan.operations], ["delete", "append", "reorder"])
        self.assertEqual(plan.operations[0].target["queries"], ["旧风险"])
        self.assertEqual(plan.operations[1].target["queries"], ["验收标准"])
        self.assertEqual(plan.operations[2].target["queries"], ["下一步建议"])

    def test_planner_fallback_detects_media_table_and_layout_ops(self) -> None:
        media_plan = ArtifactEditPlanner.from_instruction(
            artifact_type="canvas",
            instruction="在风险节点旁边新增一张示意图，并把风险节点移到右侧顶部对齐",
            available_targets=["风险节点"],
        )
        self.assertEqual([operation.op_type for operation in media_plan.operations], ["add_media", "update_layout"])
        self.assertEqual(media_plan.operations[0].payload["media_kind"], "image")
        self.assertEqual(media_plan.operations[1].payload["position"], "right")
        self.assertEqual(media_plan.operations[1].payload["align"], "top")

        table_plan = ArtifactEditPlanner.from_instruction(
            artifact_type="slides",
            instruction="补充一个表格总结当前状态",
            available_targets=["P1", "P2"],
        )
        self.assertEqual(table_plan.operations[0].op_type, "add_table")

    def test_doc_delete_after_keeps_anchor_range_semantics(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="将文档里“后续计划”后面的内容全部删除",
            available_targets=["项目背景", "后续计划", "Update (2026-04-29 15:43)"],
        )

        target = plan.operations[0].target
        self.assertEqual(target["kind"], "anchor_range")
        self.assertEqual(target["anchor"], "后续计划")
        self.assertEqual(target["scope"], "after")
        self.assertFalse(target["include_anchor"])

    def test_doc_delete_group_keeps_group_semantics(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="删除文档里 Update (2026-04-29 15:43) 这一组内容",
            available_targets=["项目背景", "Update (2026-04-29 15:43)", "refresh: 项目背景"],
        )

        target = plan.operations[0].target
        self.assertEqual(target["kind"], "anchor_range")
        self.assertEqual(target["anchor"], "Update (2026-04-29 15:43)")
        self.assertEqual(target["scope"], "group")
        self.assertTrue(target["include_anchor"])

    def test_doc_delete_between_keeps_stop_anchor(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="删除“后续计划”和“风险说明”之间的内容",
            available_targets=["项目背景", "后续计划", "风险说明"],
        )

        target = plan.operations[0].target
        self.assertEqual(target["scope"], "between")
        self.assertEqual(target["anchor"], "后续计划")
        self.assertEqual(target["stop_at"], "风险说明")

    def test_doc_clear_section_body_keeps_body_scope(self) -> None:
        plan = ArtifactEditPlanner.from_instruction(
            artifact_type="doc",
            instruction="清空“后续计划”下面的内容",
            available_targets=["项目背景", "后续计划", "风险说明"],
        )

        target = plan.operations[0].target
        self.assertEqual(plan.operations[0].op_type, "delete")
        self.assertEqual(target["kind"], "anchor_range")
        self.assertEqual(target["anchor"], "后续计划")
        self.assertEqual(target["scope"], "body")
        self.assertFalse(target["include_anchor"])

    def test_presentation_tool_uses_edit_plan_for_visible_revision(self) -> None:
        tool = PresentationTool(artifact_service=PresentationArtifactService())
        package = {
            "theme": "报名汇报",
            "slides": [
                {"title": "背景", "bullets": ["目标"], "speaker_notes": "旧讲稿"},
                {"title": "风险", "bullets": ["风险 A"], "speaker_notes": "旧风险"},
            ],
        }
        edit_plan = tool.plan_revision(package, "删除第 2 页")
        revised = tool.revise_deterministic(package, "删除第 2 页", edit_plan=edit_plan)

        self.assertTrue(tool.package_changed(package, revised))
        self.assertEqual([slide["title"] for slide in revised["slides"]], ["背景"])
        self.assertTrue(revised["artifact_edit_plan"]["mutation_required"])

    def test_presentation_structured_target_is_not_polluted_by_instruction_page_hint(self) -> None:
        tool = PresentationTool(artifact_service=PresentationArtifactService())
        package = {
            "theme": "Demo",
            "slides": [
                {"title": "Intro", "bullets": ["Keep"]},
                {"title": "Risks", "bullets": ["Remove"]},
            ],
        }
        edit_plan = tool.plan_revision(
            package,
            "Apply the plan from P1.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "slides",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "delete",
                            "target": {"kind": "slide", "query": "Risks"},
                        }
                    ],
                }
            },
        )
        revised = tool.revise_deterministic(package, "Apply the plan from P1.", edit_plan=edit_plan)

        self.assertEqual([slide["title"] for slide in revised["slides"]], ["Intro"])

    def test_presentation_structured_missing_target_does_not_rewrite_all_slides(self) -> None:
        tool = PresentationTool(artifact_service=PresentationArtifactService())
        package = {
            "theme": "Demo",
            "slides": [
                {"title": "Intro", "bullets": ["Keep"]},
                {"title": "Risks", "bullets": ["Keep risk"]},
            ],
        }
        edit_plan = tool.plan_revision(
            package,
            "Apply the approved slides plan.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "slides",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "update",
                            "target": {"kind": "slide", "query": "Missing Slide"},
                        }
                    ],
                }
            },
        )
        revised = tool.revise_deterministic(package, "Apply the approved slides plan.", edit_plan=edit_plan)

        self.assertFalse(tool.package_changed(package, revised))

    def test_presentation_tool_supports_media_table_and_layout_ops(self) -> None:
        tool = PresentationTool(artifact_service=PresentationArtifactService())
        package = {
            "theme": "Demo",
            "slides": [
                {"title": "Overview", "bullets": ["Keep"], "speaker_notes": "Intro"},
            ],
        }
        edit_plan = tool.plan_revision(
            package,
            "Apply the rich media plan.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "slides",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "add_media",
                            "target": {"kind": "slide", "query": "Overview"},
                            "payload": {"media_kind": "image", "caption": "业务流程图"},
                        },
                        {
                            "type": "add_table",
                            "target": {"kind": "slide", "query": "Overview"},
                            "payload": {"title": "状态汇总", "rows": [["模块", "状态"], ["Workbench", "开发中"]]},
                        },
                        {
                            "type": "update_layout",
                            "target": {"kind": "slide", "query": "Overview"},
                            "payload": {"position": "right", "align": "top", "instruction": "调整为左右布局"},
                        },
                    ],
                }
            },
        )
        revised = tool.revise_deterministic(package, "Apply the rich media plan.", edit_plan=edit_plan)

        slide = revised["slides"][0]
        self.assertIn("插图：业务流程图", slide["bullets"])
        self.assertEqual(slide["table"][1][0], "Workbench")
        self.assertEqual(slide["layout_hint"]["position"], "right")
        self.assertEqual(slide["layout_hint"]["align"], "top")

    def test_canvas_tool_uses_edit_plan_for_visible_revision(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        scene = {
            "title": "架构图",
            "shapes": [
                {"id": "n1", "type": "node", "text": "入口"},
                {"id": "n2", "type": "node", "text": "旧模块"},
            ],
        }
        edit_plan = tool.plan_revision(scene, "删除旧模块")
        revised = tool.revise_scene_deterministic(scene, "删除旧模块", edit_plan=edit_plan)

        self.assertEqual([shape["text"] for shape in revised["shapes"]], ["入口"])
        self.assertTrue(revised["artifact_edit_plan"]["mutation_required"])

    def test_canvas_structured_target_accepts_single_query_field(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        scene = {
            "title": "Flow",
            "shapes": [
                {"id": "n1", "type": "node", "text": "Entry"},
                {"id": "n2", "type": "node", "text": "Old Module"},
            ],
        }
        edit_plan = tool.plan_revision(
            scene,
            "Apply the approved canvas plan.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "canvas",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "delete",
                            "target": {"kind": "node", "query": "Old Module"},
                        }
                    ],
                }
            },
        )
        revised = tool.revise_scene_deterministic(scene, "Apply the approved canvas plan.", edit_plan=edit_plan)

        self.assertEqual([shape["text"] for shape in revised["shapes"]], ["Entry"])

    def test_canvas_structured_missing_target_does_not_rewrite_all_nodes(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        scene = {
            "title": "Flow",
            "shapes": [
                {"id": "n1", "type": "node", "text": "Entry"},
                {"id": "n2", "type": "node", "text": "Planner"},
            ],
        }
        edit_plan = tool.plan_revision(
            scene,
            "Apply the approved canvas plan.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "canvas",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "update",
                            "target": {"kind": "node", "query": "Missing Node"},
                        }
                    ],
                }
            },
        )
        revised = tool.revise_scene_deterministic(scene, "Apply the approved canvas plan.", edit_plan=edit_plan)

        self.assertEqual([shape["text"] for shape in revised["shapes"]], ["Entry", "Planner"])

    def test_canvas_tool_supports_media_table_and_layout_ops(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        scene = {
            "title": "Flow",
            "shapes": [
                {"id": "n1", "type": "node", "text": "Risk Panel", "x": 100, "y": 120},
                {"id": "n2", "type": "node", "text": "Summary", "x": 320, "y": 220},
            ],
        }
        edit_plan = tool.plan_revision(
            scene,
            "Apply the approved canvas media plan.",
            {
                "artifact_edit_plan": {
                    "artifact_type": "canvas",
                    "mutation_required": True,
                    "ops": [
                        {
                            "type": "add_media",
                            "payload": {"media_kind": "image", "caption": "流程示意图"},
                        },
                        {
                            "type": "add_table",
                            "payload": {"title": "状态矩阵", "rows": [["模块", "状态"], ["Canvas", "处理中"]]},
                        },
                        {
                            "type": "update_layout",
                            "target": {"kind": "node", "query": "Risk Panel"},
                            "payload": {"position": "right", "align": "top", "instruction": "移到右侧顶部"},
                        },
                    ],
                }
            },
        )
        revised = tool.revise_scene_deterministic(scene, "Apply the approved canvas media plan.", edit_plan=edit_plan)

        self.assertEqual(revised["shapes"][0]["layout_hint"]["position"], "right")
        self.assertEqual(revised["shapes"][0]["layout_hint"]["align"], "top")
        self.assertEqual(revised["shapes"][2]["group"], "Media")
        self.assertEqual(revised["shapes"][3]["group"], "Table")
        self.assertEqual(revised["shapes"][3]["table_rows"][1][0], "Canvas")


if __name__ == "__main__":
    unittest.main()
