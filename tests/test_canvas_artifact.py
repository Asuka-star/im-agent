import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.tools.canvas_tool import CanvasTool
from app.services.feishu_workflow import FeishuWorkflowService


class CanvasArtifactTests(unittest.TestCase):
    def test_canvas_service_persists_flow_scene(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="login, planner, delivery",
                llm_result={},
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

            self.assertEqual(artifact["artifact_type"], "canvas")
            self.assertEqual(artifact["url"], "/api/artifacts/canvas/Architecture-run_canvas.html")
            self.assertEqual(artifact["export_url"], "/api/artifacts/canvas/Architecture-run_canvas.svg")
            self.assertTrue((Path(tmpdir) / "Architecture-run_canvas.json").is_file())
            self.assertTrue((Path(tmpdir) / "Architecture-run_canvas.svg").is_file())
            self.assertTrue((Path(tmpdir) / "Architecture-run_canvas.html").is_file())
            self.assertEqual(artifact["preview"]["exports"]["json"], "/api/artifacts/canvas/Architecture-run_canvas.json")
            self.assertEqual(artifact["preview"]["exports"]["svg"], "/api/artifacts/canvas/Architecture-run_canvas.svg")
            self.assertEqual(artifact["preview"]["exports"]["html"], "/api/artifacts/canvas/Architecture-run_canvas.html")
            self.assertTrue(artifact["preview"]["shapes"])
            first_node = next(shape for shape in artifact["preview"]["shapes"] if shape["type"] == "node")
            self.assertEqual(first_node["color"], "#EAF5FF")
            self.assertEqual(first_node["stroke"], "#5A9FD6")
            self.assertEqual(first_node["group"], "Input")
            nodes = [shape for shape in artifact["preview"]["shapes"] if shape["type"] == "node"]
            self.assertEqual(nodes[-1]["group"], "Artifact")
            svg = (Path(tmpdir) / "Architecture-run_canvas.svg").read_text(encoding="utf-8")
            self.assertIn("<svg", svg)
            self.assertIn("login", svg)
            html = (Path(tmpdir) / "Architecture-run_canvas.html").read_text(encoding="utf-8")
            self.assertIn("自由画布预览", html)
            self.assertNotIn("鑷", html)
            self.assertIn("<svg", html)

    def test_canvas_service_uses_semantic_chinese_filename_with_short_run_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="产品流程图",
                instruction="根据刚才的需求画一张产品流程图",
                llm_result={},
                workspace_context=(
                    "[当前协作文档]\n"
                    "标题：协同产出 - 校园活动报名与审核系统需求方案 - 统计至2026-05-06 13:58\n"
                    "产品流程：学生查看活动列表，提交报名信息，负责人审核。"
                ),
                task_run_id="run_b1087928b3a5abcdef123456",
                session_id="s1",
            )

        self.assertEqual(
            artifact["url"],
            "/api/artifacts/canvas/校园活动报名与审核系统需求流程图-run_b1087928b3a5.html",
        )
        self.assertEqual(
            artifact["preview"]["exports"]["json"],
            "/api/artifacts/canvas/校园活动报名与审核系统需求流程图-run_b1087928b3a5.json",
        )

    def test_canvas_service_derives_flow_nodes_from_discussion_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            context = (
                "[\u534f\u4f5c\u4e0a\u4e0b\u6587]\n"
                "[\u8fd1\u671f\u7fa4\u804a\u8ba8\u8bba]\n"
                "- \u53d1\u8a00\u4eba Zeleous | \u5185\u5bb9: "
                "\u5f53\u524d\u7aef\u4e0eUI\u8bbe\u8ba1\u5b8c\u6210\u540e\uff0c"
                "\u518d\u7b49\u540e\u7aef\u5b8c\u6210\uff0c"
                "\u90a3\u4e48\u6211\u4eec\u7684\u9879\u76ee\u5c31\u53ef\u4ee5\u4e0a\u7ebf\u4e86"
            )
            artifact = service.generate_flow(
                title="\u6d41\u7a0b\u753b\u5e03",
                instruction="\u6839\u636e\u8ba8\u8bba\u6765\u751f\u6210\u4e00\u4e2a\u6d41\u7a0b\u753b\u5e03",
                llm_result={},
                workspace_context=context,
                task_run_id="run_canvas",
                session_id="s1",
            )

        labels = [shape["text"] for shape in artifact["preview"]["shapes"] if shape["type"] == "node"]
        self.assertEqual(
            labels,
            [
                "\u524d\u7aef\u4e0e UI \u8bbe\u8ba1\u5b8c\u6210",
                "\u540e\u7aef\u5b8c\u6210",
                "\u9879\u76ee\u4e0a\u7ebf",
            ],
        )
        self.assertNotIn("\u534f\u4f5c\u4e0a\u4e0b\u6587", " ".join(labels))
        self.assertNotIn("\u53d1\u8a00\u4eba", " ".join(labels))

    def test_canvas_service_uses_risk_template_for_risk_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="风险画布",
                instruction="把项目延期、飞书 API 权限和验收材料不足整理成风险应对图",
                llm_result={},
                workspace_context="",
                task_run_id="run_risk",
                session_id="s1",
            )

        preview = artifact["preview"]
        self.assertEqual(preview["template"], "risk")
        self.assertEqual(preview["summary"]["template"], "risk")
        groups = {shape.get("group") for shape in preview["shapes"] if shape["type"] != "arrow"}
        self.assertIn("风险", groups)
        self.assertIn("应对", groups)
        self.assertTrue(any(shape.get("label") == "缓解" for shape in preview["shapes"] if shape["type"] == "arrow"))

    def test_canvas_service_prioritizes_product_flow_over_risk_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            context = "\n".join(
                [
                    "[当前协作文档]",
                    "- 背景与痛点",
                    "  - 报名高峰期接口压力较大，活动审核规则需要老师确认。",
                    "- 产品流程",
                    "  - 1. 学生查看活动列表，选择活动并提交报名信息。",
                    "  - 2. 社团负责人审核报名申请，通过后生成正式名单。",
                    "  - 3. 负责人导出名单，学院老师查看统计结果和风险提醒。",
                    "- 风险与约束",
                    "  - 报名高峰期接口压力较大。",
                ]
            )

            artifact = service.generate_flow(
                title="产品流程图",
                instruction="根据刚才的需求画一张产品流程图",
                llm_result={},
                workspace_context=context,
                task_run_id="run_flow",
                session_id="s1",
            )

        preview = artifact["preview"]
        self.assertEqual(preview["template"], "flow")
        labels = [shape["text"] for shape in preview["shapes"] if shape["type"] == "node"]
        joined = " ".join(labels)
        self.assertIn("学生查看活动列表", joined)
        self.assertIn("负责人审核报名申请", joined)
        self.assertIn("学院老师查看统计结果和风险提醒", joined)
        self.assertNotIn("应对：明确负责人、截止时间和可验证结果", joined)

    def test_canvas_service_discards_llm_risk_shapes_for_explicit_product_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            context = "\n".join(
                [
                    "[当前协作文档]",
                    "- 产品流程",
                    "  - 1. 学生查看活动列表，选择活动并提交报名信息。",
                    "  - 2. 社团负责人审核报名申请，通过后生成正式名单。",
                    "  - 3. 负责人导出名单，学院老师查看统计结果和风险提醒。",
                    "- 风险与约束",
                    "  - 报名高峰期接口压力较大。",
                ]
            )

            artifact = service.generate_flow(
                title="产品流程图",
                instruction="根据刚才的需求画一张产品流程图",
                llm_result={
                    "canvas": {
                        "template": "risk",
                        "shapes": [
                            {
                                "id": "r1",
                                "type": "sticky",
                                "text": "风险：学生要快速报名",
                                "x": 80,
                                "y": 88,
                                "group": "风险",
                            },
                            {
                                "id": "m1",
                                "type": "node",
                                "text": "应对：明确负责人、截止时间和可验证结果",
                                "x": 380,
                                "y": 88,
                                "group": "应对",
                            },
                            {"id": "a1", "type": "arrow", "from": "r1", "to": "m1", "label": "缓解"},
                        ],
                    }
                },
                workspace_context=context,
                task_run_id="run_flow_override",
                session_id="s1",
            )

        preview = artifact["preview"]
        self.assertEqual(preview["template"], "flow")
        groups = {shape.get("group") for shape in preview["shapes"] if shape["type"] != "arrow"}
        self.assertNotIn("风险", groups)
        self.assertNotIn("应对", groups)
        labels = [shape["text"] for shape in preview["shapes"] if shape["type"] == "node"]
        joined = " ".join(labels)
        self.assertIn("学生查看活动列表", joined)
        self.assertIn("负责人导出名单", joined)
        self.assertNotIn("应对：明确负责人、截止时间和可验证结果", joined)

    def test_canvas_svg_wraps_long_node_text_inside_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="产品流程图",
                instruction="根据刚才的需求画一张产品流程图",
                llm_result={
                    "canvas": {
                        "shapes": [
                            {
                                "id": "n1",
                                "type": "node",
                                "text": "学生在活动报名高峰期查看活动列表并提交完整报名信息",
                                "x": 80,
                                "y": 140,
                                "w": 168,
                                "h": 72,
                                "group": "流程",
                            }
                        ]
                    }
                },
                workspace_context="",
                task_run_id="run_wrap",
                session_id="s1",
            )

            svg_filename = artifact["export_url"].rsplit("/", 1)[-1]
            svg = (Path(tmpdir) / svg_filename).read_text(encoding="utf-8")

        node = artifact["preview"]["shapes"][0]
        self.assertGreater(node["h"], 72)
        self.assertIn("<tspan", svg)
        self.assertNotIn(">学生在活动报名高峰期查看活动列表并提交完整报名信息</text>", svg)

    def test_canvas_service_uses_module_template_for_architecture_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="模块图",
                instruction="画一个前端、后端、测试和交付模块图",
                llm_result={},
                workspace_context="",
                task_run_id="run_module",
                session_id="s1",
            )

        preview = artifact["preview"]
        self.assertEqual(preview["template"], "module")
        labels = [shape["text"] for shape in preview["shapes"] if shape["type"] != "arrow"]
        self.assertIn("前端体验", labels)
        self.assertIn("后端服务", labels)
        self.assertIn("测试验收", labels)
        self.assertIn("集成交付与验收", labels)

    def test_canvas_tool_formats_public_preview_url(self) -> None:
        tool = CanvasTool(artifact_service=CanvasArtifactService())
        reply = tool.format_reply(
            {
                "title": "Canvas",
                "url": "/api/artifacts/canvas/run_canvas.html",
                "preview": {"shapes": [{"type": "node"}]},
            }
        )

        self.assertIn("http://science.topviewclub.cn/api/artifacts/canvas/run_canvas.html", reply)

    def test_canvas_service_normalizes_shape_style_and_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="draw architecture",
                llm_result={
                    "canvas": {
                        "shapes": [
                            {
                                "id": "n1",
                                "type": "node",
                                "text": "Start",
                                "color": "#ffeeaa",
                                "stroke": "#112233",
                                "group": "Intake",
                            },
                            {
                                "id": "a1",
                                "type": "arrow",
                                "from": "n1",
                                "to": "n2",
                                "color": "#334455",
                            },
                        ]
                    }
                },
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

        node = artifact["preview"]["shapes"][0]
        arrow = artifact["preview"]["shapes"][1]
        self.assertEqual(node["color"], "#FFEEAA")
        self.assertEqual(node["stroke"], "#112233")
        self.assertEqual(node["group"], "Intake")
        self.assertEqual(arrow["color"], "#334455")

    def test_canvas_service_preserves_revision_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="revise canvas",
                llm_result={
                    "canvas": {
                        "title": "Architecture",
                        "version": 2,
                        "revision_instruction": "revise canvas",
                        "artifact_edit_plan": {"operations": [{"type": "update"}]},
                        "shapes": [{"id": "n1", "type": "node", "text": "Updated"}],
                    }
                },
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

        preview = artifact["preview"]
        self.assertEqual(artifact["version"], 2)
        self.assertEqual(preview["version"], 2)
        self.assertEqual(preview["revision_instruction"], "revise canvas")
        self.assertEqual(preview["artifact_edit_plan"]["operations"][0]["type"], "update")

    def test_canvas_service_tolerates_non_numeric_shape_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = CanvasArtifactService(root_dir=Path(tmpdir))
            artifact = service.generate_flow(
                title="Architecture",
                instruction="draw architecture",
                llm_result={
                    "canvas": {
                        "shapes": [
                            {"id": "n1", "type": "node", "text": "Start", "x": "left", "y": "top"}
                        ]
                    }
                },
                workspace_context="",
                task_run_id="run_canvas",
                session_id="s1",
            )

        shape = artifact["preview"]["shapes"][0]
        self.assertEqual(shape["x"], 80)
        self.assertEqual(shape["y"], 140)

    def test_workflow_canvas_protocol_executes_canvas_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = FeishuWorkflowService()
            workflow.canvas_artifact_service = CanvasArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(
                session_id="s1",
                message_id="m1",
                text="draw an architecture diagram",
                chat_id=None,
            )

            result = workflow.execution_runner.execute_llm_request(
                message,
                {
                    "operation": "create",
                    "object": "canvas",
                    "route": "canvas",
                    "canvas": {"title": "Architecture", "nodes": ["IM", "Planner", "Artifact"]},
                },
                workspace_context="",
                active_episode_id=None,
                task_run_id=None,
            )

        artifact = next(item for item in result["artifacts"] if item["artifact_type"] == "canvas")
        self.assertEqual(result["mode"], "canvas")
        self.assertEqual(artifact["title"], "Architecture")
        self.assertEqual(artifact["preview"]["schema"], "im-agent.canvas.v1")

    def test_workflow_fallback_canvas_request_creates_canvas_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = FeishuWorkflowService()
            workflow.canvas_artifact_service = CanvasArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(
                session_id="s1",
                message_id="m1",
                text="帮我画一个流程图",
                chat_id=None,
                chat_type="group",
            )

            result = workflow.fallback_handler.handle_fallback_request(
                message,
                active_episode_id=None,
                task_run_id=None,
            )

        artifact = next(item for item in result["artifacts"] if item["artifact_type"] == "canvas")
        self.assertEqual(result["mode"], "canvas")
        self.assertEqual(artifact["preview"]["schema"], "im-agent.canvas.v1")


if __name__ == "__main__":
    unittest.main()
