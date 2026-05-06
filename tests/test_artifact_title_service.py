from __future__ import annotations

import unittest

from app.services.artifact_title_service import ArtifactTitleService


class ArtifactTitleServiceTests(unittest.TestCase):
    def test_canvas_title_uses_requirement_subject_from_current_doc_title(self) -> None:
        context = (
            "[当前协作文档]\n"
            "标题：协同产出 - 校园活动报名与审核系统需求方案 - 统计至2026-05-06 13:58\n"
            "产品流程：学生查看活动列表，提交报名信息，负责人审核。"
        )

        title = ArtifactTitleService.canvas_title(
            current_title="产品流程图",
            instruction="根据刚才的需求画一张产品流程图",
            workspace_context=context,
            template="flow",
        )

        self.assertEqual(title, "校园活动报名与审核系统需求流程图")

    def test_presentation_title_uses_requirement_subject_from_current_doc_title(self) -> None:
        context = (
            "[当前协作文档]\n"
            "标题：协同产出 - 校园活动报名与审核系统需求方案 - 统计至2026-05-06 13:58\n"
            "技术方案：飞书 IM 负责入口，Agent 负责理解和规划。"
        )

        title = ArtifactTitleService.presentation_title(
            current_title="Presentation",
            instruction="基于这份需求方案文档生成一份正式答辩 PPT",
            workspace_context=context,
        )

        self.assertEqual(title, "校园活动报名与审核系统答辩演示稿")

    def test_title_service_extracts_subject_from_discussion_project_phrase(self) -> None:
        title = ArtifactTitleService.canvas_title(
            current_title="流程图",
            instruction="根据刚才的需求画一张产品流程图",
            workspace_context="我们这次想做一个校园活动报名与审核系统，主要解决社团活动报名信息分散的问题。",
            template="flow",
        )

        self.assertEqual(title, "校园活动报名与审核系统需求流程图")

    def test_presentation_title_replaces_generic_fallback_theme_when_subject_missing(self) -> None:
        title = ArtifactTitleService.presentation_title(
            current_title="基于飞书群聊讨论的需求方案与正式汇报",
            instruction="生成一份正式 PPT",
            workspace_context="目标用户和核心功能还在讨论中。",
        )

        self.assertEqual(title, "汇报演示稿")


if __name__ == "__main__":
    unittest.main()
