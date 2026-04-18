import unittest
from unittest.mock import patch

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.db.models import Task
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.interaction import InteractionService


class DocSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = FeishuWorkflowService()
        self.interaction = InteractionService()
        self.memory_service = MemoryService()

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

    def test_default_doc_title_includes_timestamp(self) -> None:
        with patch.object(self.workflow, "_doc_title_timestamp", return_value="2026-04-17 11:30"):
            title = self.workflow._default_doc_title("帮我把这轮讨论整理成文档")
        self.assertIn("统计至2026-04-17 11:30", title)

    def test_default_doc_title_prefers_stats_cutoff_time(self) -> None:
        title = self.workflow._default_doc_title(
            "帮我把这轮讨论整理成文档",
            stats_as_of="2026-04-17 11:34",
        )
        self.assertIn("统计至2026-04-17 11:34", title)

    def test_merge_task_items_keeps_previous_unmatched_tasks(self) -> None:
        current_tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="旧任务",
            )
        ]
        refreshed_tasks = [
            TaskItem(
                title="前端开发",
                owner="张三",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="新任务",
            )
        ]
        merged = self.workflow._merge_task_items(current_tasks, refreshed_tasks)
        self.assertEqual(len(merged), 2)
        self.assertTrue(any(task.title == "后端开发" and task.owner == "张三" for task in merged))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "张三" for task in merged))

    def test_update_current_tasks_from_discussion_prefers_explicit_new_assignment(self) -> None:
        current_tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="张三4月30号之前搞定后端",
            ),
            TaskItem(
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="李四4月29号之前搞定前端",
            ),
        ]
        updated = self.workflow._update_current_tasks_from_discussion(
            current_tasks,
            "张三你也去搞前端吧",
            llm_tasks=[],
        )
        self.assertTrue(any(task.title == "后端开发" and task.owner == "张三" for task in updated))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "李四" for task in updated))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "张三" for task in updated))

    def test_merge_current_tasks_can_drop_removed_items_when_snapshot_is_exact(self) -> None:
        previous_tasks = [
            Task(
                id=1,
                session_id="s1",
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="旧任务",
            ),
            Task(
                id=2,
                session_id="s1",
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="保留任务",
            ),
        ]
        current_tasks = [
            TaskItem(
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="保留任务",
            )
        ]

        merged = self.memory_service._merge_current_tasks(
            previous_tasks,
            current_tasks,
            preserve_unmatched_previous=False,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "前端开发")

if __name__ == "__main__":
    unittest.main()
