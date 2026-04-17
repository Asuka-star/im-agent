import unittest

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.task import TaskItem
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.interaction import InteractionService


class DocSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = FeishuWorkflowService()
        self.interaction = InteractionService()

    def test_interaction_service_detects_doc_request(self) -> None:
        decision = self.interaction.decide("帮我把这轮讨论整理成飞书文档")
        self.assertEqual(decision.mode, "doc")

    def test_document_from_analysis_contains_core_sections(self) -> None:
        analysis = AnalyzeResponse(
            session_id="s1",
            summary="讨论明确了后端和前端分工。",
            tasks=[
                TaskItem(
                    title="后端开发",
                    owner="张三",
                    priority="high",
                    due_date="2026-04-18",
                    status="draft",
                    notes="负责接口联调",
                )
            ],
            risks=["后端时间较紧"],
            next_actions=["张三先完成接口联调"],
            agent_traces=[AgentTrace(agent="planner", summary="ok")],
        )

        package = self.workflow._document_from_analysis(analysis, "把这轮讨论整理成文档")
        self.assertIn("title", package)
        self.assertTrue(package["sections"])
        headings = [section["heading"] for section in package["sections"]]
        self.assertIn("讨论摘要", headings)
        self.assertIn("任务清单", headings)


if __name__ == "__main__":
    unittest.main()
