import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import settings
from app.schemas.task import TaskItem
from app.services.feishu_workflow import FeishuWorkflowService


class LLMTaskOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FeishuWorkflowService()
        self.reply_enabled_patcher = patch.object(settings, "feishu_reply_enabled", False)
        self.reply_enabled_patcher.start()
        self.addCleanup(self.reply_enabled_patcher.stop)

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
            self.service.memory_service,
            "close_active_episode",
        ), patch.object(
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

    def test_clarification_request_creates_confirmation_and_pauses_run(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "帮我把这个写成文档", "chat_id": "c1"},
        )()
        with patch.object(self.service.task_run_service, "update_task_run") as update_task_run, patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "create_confirmation",
        ) as create_confirmation, patch.object(
            self.service.task_run_service,
            "merge_task_run_metadata",
        ):
            result = self.service._execute_llm_request(
                message,
                {
                    "intent": "doc",
                    "reason": "用户想要形成正式文档",
                    "next_actions": ["先确认文档面向谁"],
                    "clarification": {
                        "needed": True,
                        "question": "这份文档是用于报名材料，还是用于组内评审？",
                        "reason": "两种写法差异较大",
                        "options": ["报名材料版", "组内评审版"],
                        "blocking": True,
                    },
                },
                "[workspace]",
                1,
                task_run_id="run_123",
            )

        self.assertEqual(result["mode"], "doc")
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["task_run_status"], "waiting_confirmation")
        self.assertEqual(result["response_step_status"], "pending")
        self.assertTrue(any(item["artifact_type"] == "agent_plan" for item in result["artifacts"]))
        update_task_run.assert_any_call(
            "run_123",
            stage="awaiting_user_confirmation",
            status="waiting_confirmation",
        )
        upsert_step.assert_any_call(
            "run_123",
            step_key="user_confirmation",
            title="等待用户确认",
            step_type="confirmation",
            status="pending",
            output_payload={
                "question": "这份文档是用于报名材料，还是用于组内评审？",
                "reason": "两种写法差异较大",
                "options": ["报名材料版", "组内评审版"],
            },
        )
        create_confirmation.assert_called_once_with(
            "run_123",
            prompt="这份文档是用于报名材料，还是用于组内评审？",
            options=["报名材料版", "组内评审版"],
        )

    def test_handle_message_preserves_waiting_confirmation_status(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "s1",
                "message_id": "m1",
                "sender_id": "u1",
                "sender_user_id": "user_1",
                "sender_open_id": "open_1",
                "sender_union_id": "union_1",
                "text": "@bot 帮我整理成文档",
                "raw_text": "@bot 帮我整理成文档",
                "chat_id": "c1",
                "chat_type": "group",
                "is_mentioned": True,
                "mentioned_users": [],
                "event_id": "evt_1",
            },
        )()
        with patch.object(self.service, "_ensure_sender_alias"), patch.object(
            self.service.memory_service,
            "save_user_message",
        ), patch.object(
            self.service.memory_service,
            "save_assistant_message",
        ), patch.object(
            self.service.task_run_service,
            "create_task_run",
            return_value=SimpleNamespace(task_run_id="run_123"),
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "update_task_run",
        ) as update_task_run, patch.object(
            self.service.task_run_service,
            "create_artifact",
        ), patch.object(
            self.service,
            "_handle_mentioned_request",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "请先确认文档用途",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
                "pending_confirmation": True,
                "response_step_status": "pending",
                "task_run_status": "waiting_confirmation",
                "task_run_stage": "awaiting_user_confirmation",
            },
        ):
            result = self.service.handle_message(message)

        self.assertEqual(result["task_run_id"], "run_123")
        last_step_call = upsert_step.call_args_list[-1]
        self.assertEqual(last_step_call.kwargs["status"], "pending")
        last_update_call = update_task_run.call_args_list[-1]
        self.assertEqual(last_update_call.kwargs["status"], "waiting_confirmation")
        self.assertEqual(last_update_call.kwargs["stage"], "awaiting_user_confirmation")

    def test_resume_after_confirmation_replays_agent_flow(self) -> None:
        with patch.object(
            self.service.task_run_service,
            "get_task_run",
            return_value=SimpleNamespace(
                session_id="s1",
                trigger_message_id="m1",
                metadata_json="{}",
            ),
        ), patch.object(
            self.service.task_run_service,
            "get_task_run_metadata",
            return_value={
                "resume_after_confirmation": {
                    "intent": "doc",
                    "instruction": "帮我把讨论整理成报名材料",
                    "workspace_context": "[workspace]",
                    "active_episode_id": 7,
                    "question": "报名材料还是组内评审？",
                    "reason": "用途不同",
                    "options": ["报名材料版", "组内评审版"],
                    "confirmation_id": "confirm_123",
                }
            },
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "update_task_run",
        ) as update_task_run, patch.object(
            self.service.memory_service,
            "save_assistant_message",
        ), patch.object(
            self.service.task_run_service,
            "create_artifact",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
            return_value={"intent": "doc", "reason": "已拿到补充确认"},
        ) as resolve_workspace_request, patch.object(
            self.service,
            "_execute_llm_request",
            return_value={
                "session_id": "s1",
                "episode_id": 7,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "已继续生成文档",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ) as execute_llm_request:
            result = self.service.resume_task_run_after_confirmation(
                "run_123",
                confirmation_id="confirm_123",
                answer_value="报名材料版",
                answered_by="tester",
            )

        self.assertIsNotNone(result)
        resolve_workspace_request.assert_called_once()
        resumed_instruction = resolve_workspace_request.call_args.args[1]
        self.assertIn("报名材料版", resumed_instruction)
        self.assertIn("用户刚刚确认", resumed_instruction)
        execute_llm_request.assert_called_once()
        first_update = update_task_run.call_args_list[0]
        self.assertEqual(first_update.kwargs["stage"], "confirmation_replanning")
        self.assertEqual(first_update.kwargs["status"], "running")
        upsert_step.assert_any_call(
            "run_123",
            step_key="user_confirmation",
            title="等待用户确认",
            step_type="confirmation",
            status="done",
            output_payload={
                "question": "报名材料还是组内评审？",
                "reason": "用途不同",
                "options": ["报名材料版", "组内评审版"],
                "answer_value": "报名材料版",
                "answered_by": "tester",
            },
        )


if __name__ == "__main__":
    unittest.main()
