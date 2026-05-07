import unittest

from app.services.offline_document_merge import OfflineDocumentMergeService


class OfflineDocumentMergeServiceTests(unittest.TestCase):
    def test_summary_detects_updated_new_and_rich_blocks(self) -> None:
        service = OfflineDocumentMergeService()
        package = {
            "title": "offline",
            "sections": [
                {
                    "heading": "讨论摘要",
                    "paragraphs": [
                        "新摘要",
                        {"type": "image", "token": "img_1", "caption": "流程图"},
                    ],
                },
                {
                    "heading": "验收标准",
                    "paragraphs": [
                        {"type": "table", "rows": [["模块", "状态"], ["Workbench", "开发中"]]},
                    ],
                },
            ],
        }
        current_document = {
            "document_id": "doc_current",
            "title": "Current Document",
            "version": 3,
            "section_snapshot": [
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "风险与卡点", "paragraphs": ["旧风险"]},
            ],
        }

        summary = service.summarize_for_confirmation(package, current_document=current_document)

        self.assertEqual(summary["base_document_id"], "doc_current")
        self.assertEqual(summary["base_document_version"], 3)
        self.assertEqual(summary["updated_headings"], ["讨论摘要"])
        self.assertEqual(summary["new_headings"], ["验收标准"])
        self.assertEqual(summary["image_count"], 1)
        self.assertEqual(summary["table_count"], 1)
        self.assertIn("拟更新章节 1 个", summary["summary_lines"][0])

    def test_summary_warns_when_no_current_document_or_changes(self) -> None:
        service = OfflineDocumentMergeService()
        package = {
            "title": "offline",
            "sections": [{"heading": "讨论摘要", "paragraphs": ["同样的摘要"]}],
        }
        current_document = {
            "document_id": "",
            "section_snapshot": [{"heading": "讨论摘要", "paragraphs": ["同样的摘要"]}],
        }

        summary = service.summarize_for_confirmation(package, current_document=current_document)

        self.assertIn("current_document_missing", summary["warning_flags"])
        self.assertIn("no_structural_changes_detected", summary["warning_flags"])


    def test_merge_plan_marks_high_delta_section_conflicts(self) -> None:
        service = OfflineDocumentMergeService()
        package = {
            "title": "offline",
            "sections": [
                {
                    "heading": "方案设计",
                    "paragraphs": [
                        "新的方案改成需求工作区优先，飞书主文档只保留最终结论。",
                        "同时把离线回传确认改成五档动作，不再沿用旧的二选一。",
                        "本次还会补一张新的结构表格。",
                        {"type": "table", "rows": [["模块", "动作"], ["Workbench", "增加离线队列"]]},
                    ],
                }
            ],
        }
        current_document = {
            "document_id": "doc_current",
            "title": "Current Document",
            "version": 9,
            "section_snapshot": [
                {
                    "heading": "方案设计",
                    "paragraphs": [
                        "当前设计仍然以飞书卡片为唯一入口。",
                        "Workbench 还不支持离线回传确认，也没有需求工作区。",
                        "PPT 和 Canvas 的同步还挂在交付包阶段。",
                    ],
                }
            ],
        }

        plan = service.build_merge_plan(package, current_document=current_document)

        self.assertEqual(plan["changed_section_count"], 1)
        self.assertGreaterEqual(plan["conflict_count"], 1)
        self.assertFalse(plan["auto_merge_eligible"])
        self.assertEqual(plan["section_operations"][0]["risk_level"], "high")
        self.assertTrue(plan["section_operations"][0]["requires_review"])
        self.assertIn("current_preview_lines", plan["section_operations"][0])
        self.assertIn("next_preview_lines", plan["section_operations"][0])
        self.assertTrue(any(item["type"] == "section_rewrite_high_delta" for item in plan["conflicts"]))

    def test_merge_plan_blocks_auto_merge_for_rich_media_conflicts(self) -> None:
        service = OfflineDocumentMergeService()
        package = {
            "title": "offline",
            "sections": [
                {
                    "heading": "总体方案",
                    "paragraphs": [
                        "保留现有方案结论，只补一张新的画布示意图。",
                        {"type": "image", "token": "img_2", "caption": "新流程图"},
                    ],
                }
            ],
        }
        current_document = {
            "document_id": "doc_current",
            "title": "Current Document",
            "version": 4,
            "section_snapshot": [
                {
                    "heading": "总体方案",
                    "paragraphs": [
                        "保留现有方案结论，只补一张新的画布示意图。",
                    ],
                }
            ],
        }

        plan = service.build_merge_plan(package, current_document=current_document)

        self.assertGreaterEqual(plan["conflict_count"], 1)
        self.assertFalse(plan["auto_merge_eligible"])
        self.assertEqual(plan["recommended_action"], "confirm_merge")
        self.assertTrue(any(item["type"] == "section_rich_media_update" for item in plan["conflicts"]))


if __name__ == "__main__":
    unittest.main()
