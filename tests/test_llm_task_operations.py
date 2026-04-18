import unittest
from unittest.mock import patch

from app.schemas.task import TaskItem
from app.services.feishu_workflow import FeishuWorkflowService


class LLMTaskOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FeishuWorkflowService()

    def test_update_operation_replaces_existing_task(self) -> None:
        current_tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="medium",
                due_date="2026-04-23",
                status="draft",
                notes="原始任务",
            )
        ]
        operations = [
            {
                "action": "update",
                "match_hint": {"title": "后端开发", "owner": "张三"},
                "task": {
                    "title": "后端开发",
                    "owner": "张三",
                    "priority": "high",
                    "due_date": "2026-04-18",
                    "status": "draft",
                    "notes": "时间提前",
                },
                "reason": "讨论里明确改了时间",
            }
        ]

        result = self.service._apply_llm_task_operations(current_tasks, operations)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].priority, "high")
        self.assertEqual(result[0].due_date, "2026-04-18")

    def test_remove_operation_drops_existing_task(self) -> None:
        current_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="前端开发", owner="李四", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        operations = [
            {
                "action": "remove",
                "match_hint": {"title": "前端开发", "owner": "李四"},
                "task": {},
                "reason": "这项任务先不做",
            }
        ]

        result = self.service._apply_llm_task_operations(current_tasks, operations)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "后端开发")

    def test_create_operation_appends_new_task(self) -> None:
        current_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        operations = [
            {
                "action": "create",
                "match_hint": {},
                "task": {
                    "title": "前端开发",
                    "owner": "李四",
                    "priority": "medium",
                    "due_date": "2026-04-22",
                    "status": "draft",
                    "notes": "新增任务",
                },
                "reason": "讨论中新增前端分工",
            }
        ]

        result = self.service._apply_llm_task_operations(current_tasks, operations)
        self.assertEqual(len(result), 2)
        self.assertTrue(any(task.title == "前端开发" and task.owner == "李四" for task in result))

    def test_llm_summary_path_saves_exact_snapshot(self) -> None:
        analysis = TaskItem(
            title="前端开发",
            owner="李四",
            priority="medium",
            due_date="2026-04-22",
            status="draft",
            notes="新增任务",
        )
        with patch.object(self.service, "_build_analysis_from_llm") as build_analysis, patch.object(
            self.service.memory_service,
            "build_discussion_block",
            return_value="李四4月22号之前搞定前端",
        ), patch.object(self.service.memory_service, "save_round") as save_round, patch.object(
            self.service,
            "_deliver_reply",
            return_value={"reply_preview": "ok", "reply_sent": False},
        ):
            build_analysis.return_value = type(
                "FakeAnalysis",
                (),
                {
                    "summary": "ok",
                    "tasks": [analysis],
                    "risks": [],
                    "next_actions": ["ok"],
                },
            )()
            self.service._execute_llm_request(
                type(
                    "FakeMessage",
                    (),
                    {"session_id": "s1", "message_id": "m1", "text": "总结一下", "chat_id": "c1"},
                )(),
                {"intent": "summary", "reason": "测试"},
                "[协作上下文]",
                1,
            )

        self.assertTrue(save_round.called)
        self.assertFalse(save_round.call_args.kwargs["preserve_unmatched_previous"])


if __name__ == "__main__":
    unittest.main()
