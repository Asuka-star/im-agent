import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.schemas.task import TaskItem
from app.services.tools.doc_tool import DocumentSyncResult
from app.services.tools.task_operation_tool import TaskOperationTool
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.presentation_artifact_service import PresentationArtifactService
from app.services.request_router import RouteDecision


class LLMTaskOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FeishuWorkflowService()
        self.reply_enabled_patcher = patch.object(settings, "feishu_reply_enabled", False)
        self.reply_enabled_patcher.start()
        self.addCleanup(self.reply_enabled_patcher.stop)
        self.workflow_engine_patcher = patch.object(settings, "workflow_engine", "legacy")
        self.workflow_engine_patcher.start()
        self.addCleanup(self.workflow_engine_patcher.stop)

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

        result = TaskOperationTool.apply_llm_operations(current_tasks, operations)
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

        result = TaskOperationTool.apply_llm_operations(current_tasks, operations)
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

        result = TaskOperationTool.apply_llm_operations(current_tasks, operations)
        self.assertEqual(len(result), 2)
        self.assertTrue(any(task.title == "前端开发" and task.owner == "李四" for task in result))

    def test_pending_completion_updates_existing_task_instead_of_extracting_dirty_task(self) -> None:
        message = SimpleNamespace(session_id="s1", message_id="m_status", chat_type="group")
        base_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        episode = SimpleNamespace(id=10)
        with patch.object(self.service.memory_service, "get_active_episode", return_value=episode), patch.object(
            self.service.memory_service,
            "get_episode_messages",
            return_value=[SimpleNamespace(content="张三的任务完成了")],
        ), patch.object(self.service.llm_service, "extract_collaboration") as extract_collaboration:
            pending_tasks = self.service._pending_discussion_tasks_for_message(message, base_tasks=base_tasks)

        self.assertEqual(len(pending_tasks), 1)
        self.assertEqual(pending_tasks[0].title, "后端开发")
        self.assertEqual(pending_tasks[0].status, "done")
        extract_collaboration.assert_not_called()

    def test_ambiguous_pending_completion_is_not_extracted_as_new_task(self) -> None:
        message = SimpleNamespace(session_id="s1", message_id="m_status", chat_type="group")
        base_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="接口联调", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        episode = SimpleNamespace(id=10)
        with patch.object(self.service.memory_service, "get_active_episode", return_value=episode), patch.object(
            self.service.memory_service,
            "get_episode_messages",
            return_value=[SimpleNamespace(content="张三的任务完成了")],
        ), patch.object(self.service.llm_service, "extract_collaboration") as extract_collaboration:
            pending_tasks = self.service._pending_discussion_tasks_for_message(message, base_tasks=base_tasks)

        self.assertEqual(pending_tasks, [])
        extract_collaboration.assert_not_called()

    def test_all_scope_pending_completion_updates_all_owner_tasks(self) -> None:
        message = SimpleNamespace(session_id="s1", message_id="m_status", chat_type="group")
        base_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="接口联调", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="前端开发", owner="李四", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        episode = SimpleNamespace(id=10)
        with patch.object(self.service.memory_service, "get_active_episode", return_value=episode), patch.object(
            self.service.memory_service,
            "get_episode_messages",
            return_value=[SimpleNamespace(content="张三的任务全部完成了")],
        ), patch.object(self.service.llm_service, "extract_collaboration") as extract_collaboration:
            pending_tasks = self.service._pending_discussion_tasks_for_message(message, base_tasks=base_tasks)

        self.assertEqual([task.status for task in pending_tasks], ["done", "done", "draft"])
        extract_collaboration.assert_not_called()

    def test_pending_first_person_completion_uses_message_sender_alias(self) -> None:
        message = SimpleNamespace(session_id="s1", message_id="m_status", chat_type="group")
        base_tasks = [
            TaskItem(title="后端开发", owner="张三", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="后端开发", owner="李四", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        episode = SimpleNamespace(id=10)

        def alias_lookup(_session_id: str, identifier: str | None) -> str | None:
            return "张三" if identifier == "sender_zhang" else None

        with patch.object(self.service.memory_service, "get_active_episode", return_value=episode), patch.object(
            self.service.memory_service,
            "get_episode_messages",
            return_value=[SimpleNamespace(content="我已完成后端开发任务", sender_id="sender_zhang")],
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            side_effect=alias_lookup,
        ), patch.object(self.service.llm_service, "extract_collaboration") as extract_collaboration:
            pending_tasks = self.service._pending_discussion_tasks_for_message(message, base_tasks=base_tasks)

        self.assertEqual(len(pending_tasks), 2)
        self.assertEqual(pending_tasks[0].owner, "张三")
        self.assertEqual(pending_tasks[0].status, "done")
        self.assertEqual(pending_tasks[1].owner, "李四")
        self.assertEqual(pending_tasks[1].status, "draft")
        extract_collaboration.assert_not_called()

    def test_local_unassigned_help_request_creates_tbd_task_without_llm(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_assign",
            text="需要有人来帮我完成后端开发任务",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_zhang",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        with patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], [], []),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="张三",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ) as deliver_reply:
            result = self.service._handle_local_task_assignment_instruction(
                message,
                route_decision=RouteDecision(route="tasks", source="rule", confidence=0.88),
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        self.assertEqual(len(analysis.tasks), 1)
        self.assertEqual(analysis.tasks[0].title, "后端开发")
        self.assertEqual(analysis.tasks[0].owner, "TBD")
        self.assertEqual(analysis.tasks[0].status, "draft")
        deliver_reply.assert_called_once()

    def test_llm_task_intent_updates_sender_owned_task_with_guardrail(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done",
            text="I wrapped up the API side",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="API integration", owner="Alice", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="API integration", owner="Bob", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "API",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.91,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ):
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        by_owner = {task.owner: task.status for task in analysis.tasks}
        self.assertEqual(by_owner["Alice"], "done")
        self.assertEqual(by_owner["Bob"], "draft")

    def test_llm_task_intent_clarifies_when_sender_has_no_matching_task(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done_no_match",
            text="I wrapped up the backend work",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_bob",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="Backend development", owner="Alice", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "backend",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.93,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Bob",
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertTrue(result["pending_confirmation"])
        pause_for_clarification.assert_called_once()
        save_round.assert_not_called()

    def test_local_status_update_clarification_uses_pending_discussion_tasks(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_done_with_pending",
            text="\u5f20\u4e09\u7684\u4efb\u52a1\u5b8c\u6210\u4e86",
            chat_id="c1",
            chat_type="group",
            sender_id="sender",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        context_tasks = [
            TaskItem(title="\u539f\u6709\u4efb\u52a1", owner="\u5f20\u4e09", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="\u505a ppt", owner="\u5f20\u4e09", priority="medium", due_date="TBD", status="draft", notes="pending"),
            TaskItem(title="\u505a canvas", owner="\u5f20\u4e09", priority="medium", due_date="TBD", status="draft", notes="pending"),
        ]

        with patch.object(
            self.service,
            "_context_tasks_for_message",
            return_value=context_tasks,
        ) as context_tasks_for_message, patch.object(
            self.service,
            "_sender_actor_names_for_message",
            return_value=["sender"],
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_task_status_update_instruction(
                message,
                route_decision=RouteDecision(route="tasks", source="rule", confidence=0.92),
                active_episode_id=27,
                task_run_id="run_1",
            )

        self.assertTrue(result["pending_confirmation"])
        context_tasks_for_message.assert_called_once_with(message)
        pause_for_clarification.assert_called_once()
        clarification = pause_for_clarification.call_args.kwargs["clarification"]
        self.assertEqual(len(clarification["candidates"]), 3)
        self.assertIn("\u505a ppt", [item["title"] for item in clarification["candidates"]])
        self.assertIn("\u505a canvas", [item["title"] for item in clarification["candidates"]])
        save_round.assert_not_called()

    def test_llm_task_intent_does_not_match_backend_to_frontend_by_common_suffix(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done_suffix",
            text="I wrapped up the backend work",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_bob",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="Frontend development", owner="Bob", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "backend",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.93,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Bob",
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertTrue(result["pending_confirmation"])
        pause_for_clarification.assert_called_once()
        save_round.assert_not_called()

    def test_llm_task_intent_does_not_match_short_ai_inside_paid(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done_ai",
            text="I wrapped up the AI task",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="Paid feature rollout", owner="Alice", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "AI",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.93,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertTrue(result["pending_confirmation"])
        pause_for_clarification.assert_called_once()
        save_round.assert_not_called()

    def test_llm_task_intent_allows_short_ascii_token_match(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done_ai_token",
            text="I wrapped up the AI task",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="AI model evaluation", owner="Alice", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "AI",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.93,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ):
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        self.assertEqual(analysis.tasks[0].status, "done")

    def test_llm_task_intent_does_not_match_single_chinese_character_hint(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_done_short_cn",
            text="图做完了",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="流程图绘制", owner="Alice", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_status_update",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "图",
                "status": "done",
                "assignee": {"source": "unknown", "text": ""},
                "confidence": 0.93,
                "requires_existing_task": True,
                "reason": "completion wording",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertTrue(result["pending_confirmation"])
        pause_for_clarification.assert_called_once()
        save_round.assert_not_called()

    def test_llm_task_intent_creates_sender_assignment_candidate(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_assign",
            text="I can take the API integration work",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_assignment",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "API integration",
                "status": "draft",
                "assignee": {"source": "sender", "text": ""},
                "confidence": 0.9,
                "requires_existing_task": False,
                "reason": "claiming task",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], [], []),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ):
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        self.assertEqual(len(analysis.tasks), 1)
        self.assertEqual(analysis.tasks[0].title, "API integration")
        self.assertEqual(analysis.tasks[0].owner, "Alice")
        self.assertEqual(analysis.tasks[0].status, "draft")

    def test_llm_task_intent_assignment_claims_existing_tbd_task(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_assign_tbd",
            text="I can take the API integration work",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_alice",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="API integration", owner="TBD", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_assignment",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "API integration",
                "status": "draft",
                "assignee": {"source": "sender", "text": ""},
                "confidence": 0.9,
                "requires_existing_task": False,
                "reason": "claiming task",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Alice",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ):
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        self.assertEqual(len(analysis.tasks), 1)
        self.assertEqual(analysis.tasks[0].owner, "Alice")

    def test_llm_task_intent_assignment_conflict_asks_clarification(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_assign_conflict",
            text="I can take the API integration work",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_bob",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="API integration", owner="Alice", priority="medium", due_date="TBD", status="draft", notes="")
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_assignment",
                "actor": {"source": "sender", "text": ""},
                "task_hint": "API integration",
                "status": "draft",
                "assignee": {"source": "sender", "text": ""},
                "confidence": 0.9,
                "requires_existing_task": False,
                "reason": "claiming task",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="Bob",
        ), patch.object(
            self.service,
            "_pause_for_clarification",
            return_value={"mode": "tasks", "pending_confirmation": True, "reply_preview": "clarify"},
        ) as pause_for_clarification, patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round:
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="unknown", source="fallback", confidence=0.0),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertTrue(result["pending_confirmation"])
        pause_for_clarification.assert_called_once()
        save_round.assert_not_called()

    def test_llm_task_intent_assignment_uses_owner_hint_to_reassign_existing_task(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_llm_assign_owner_hint",
            text="统计任务李彪由我来实现",
            chat_id="c1",
            chat_type="group",
            sender_id="sender_wang",
            sender_user_id=None,
            sender_open_id=None,
            sender_union_id=None,
        )
        current_tasks = [
            TaskItem(title="统计任务", owner="李彪", priority="medium", due_date="TBD", status="draft", notes=""),
            TaskItem(title="接口联调", owner="李彪", priority="medium", due_date="TBD", status="draft", notes=""),
        ]
        with patch.object(self.service.llm_service, "is_configured", return_value=True), patch.object(
            self.service.llm_service,
            "resolve_task_intent",
            return_value={
                "intent": "task_assignment",
                "actor": {"source": "unknown", "text": ""},
                "task_hint": "统计任务李彪",
                "target_task": {"title_hint": "统计任务", "owner_hint": "李彪"},
                "status": "draft",
                "assignee": {"source": "sender", "text": ""},
                "confidence": 0.9,
                "requires_existing_task": True,
                "reason": "sender claims an existing task from named owner",
            },
        ), patch.object(
            self.service,
            "_base_status_task_sources_for_message",
            return_value=([], current_tasks, current_tasks),
        ), patch.object(
            self.service.memory_service,
            "get_alias_display_name",
            return_value="王五",
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={"mode": "tasks", "reply_preview": "ok", "reply_sent": False, "artifacts": []},
        ):
            result = self.service._handle_llm_task_intent_instruction(
                message,
                route_decision=RouteDecision(route="tasks", source="rule", confidence=0.88),
                workspace_context="[tasks]",
                active_episode_id=None,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "tasks")
        analysis = save_round.call_args.kwargs["analysis"]
        by_title = {task.title: task.owner for task in analysis.tasks}
        self.assertEqual(by_title["统计任务"], "王五")
        self.assertEqual(by_title["接口联调"], "李彪")
        self.assertFalse(any(task.title == "统计任务李彪" for task in analysis.tasks))

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
            self.service.reply_sender,
            "deliver_reply",
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
            self.service.execution_runner.execute_llm_request(
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
            result = self.service.execution_runner.execute_llm_request(
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

    def test_langgraph_primary_runs_before_legacy_route_resolution(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_graph_first",
            chat_id="c1",
            chat_type="group",
            sender_id="ou_1",
            text="帮我总结项目进展",
            raw_text="帮我总结项目进展",
            is_mentioned=True,
        )
        graph_result = {
            "session_id": "s1",
            "episode_id": None,
            "mode": "summary",
            "analysis": None,
            "reply_preview": "graph done",
            "reply_sent": False,
            "reply_error": None,
            "artifacts": [],
        }
        with patch.object(
            settings,
            "workflow_engine",
            "langgraph",
        ), patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service,
            "_route_request",
        ) as route_request, patch.object(
            self.service.graph_runner,
            "run_task_graph",
            return_value=graph_result,
        ) as run_task_graph:
            result = self.service._handle_mentioned_request(message, task_run_id="run_graph_first")

        self.assertEqual(result["reply_preview"], "graph done")
        route_request.assert_not_called()
        run_task_graph.assert_called_once()
        self.assertIsNone(run_task_graph.call_args.kwargs["legacy_route"])

    def test_ambiguous_route_uses_lightweight_dag_clarification_before_deep_resolution(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "s1",
                "message_id": "m1",
                "text": "帮我整理一下",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()
        confirmation = SimpleNamespace(confirmation_id="confirm_route")
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "update_task_run",
        ) as update_task_run, patch.object(
            self.service.task_run_service,
            "create_confirmation",
            return_value=confirmation,
        ) as create_confirmation, patch.object(
            self.service.task_run_service,
            "merge_task_run_metadata",
        ) as merge_metadata, patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "plan_workspace_request",
            return_value={
                "operation": "unknown",
                "object": "workspace",
                "confidence": 0.31,
                "reason": "target output is unclear",
                "clarification": {
                    "needed": True,
                    "question": "Which output should I prepare?",
                    "reason": "Need the artifact type before execution.",
                    "options": ["doc", "slides", "summary"],
                    "blocking": True,
                },
            },
        ) as plan_workspace, patch.object(
            self.service.llm_service,
            "route_workspace_request",
            return_value={
                "route": "unknown",
                "confidence": 0.3,
                "needs_clarification": True,
                "reason": "request is too broad",
            },
        ) as route_workspace, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace_request, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "help",
                "analysis": None,
                "reply_preview": "需要确认",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            result = self.service._handle_mentioned_request(message, task_run_id="run_route")

        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["confirmation_id"], "confirm_route")
        route_workspace.assert_called_once_with(message.text)
        plan_workspace.assert_called_once_with("[workspace]", message.text)
        resolve_workspace_request.assert_not_called()
        create_confirmation.assert_called_once()
        self.assertIn("doc", create_confirmation.call_args.kwargs["options"])
        update_task_run.assert_any_call(
            "run_route",
            stage="awaiting_user_confirmation",
            status="waiting_confirmation",
        )
        merge_metadata.assert_called_once()
        upsert_step.assert_any_call(
            "run_route",
            step_key="request_route",
            title="确定请求路由",
            step_type="intent",
            status="done",
            output_payload={
                "route": "unknown",
                "source": "llm",
                "confidence": 0.3,
                "needs_clarification": True,
                "reason": "request is too broad",
                "requested_outputs": [],
            },
        )

    def test_doc_revision_with_multiple_session_documents_pauses_for_target_selection(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "s1",
                "message_id": "m1",
                "text": "帮我更新一下这份文档的风险部分",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()
        confirmation = SimpleNamespace(confirmation_id="confirm_doc_choice")
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service,
            "_route_request",
            return_value=RouteDecision(route="doc", source="rule", confidence=0.98),
        ), patch.object(
            self.service.session_document_service,
            "list_documents",
            return_value=[
                {"document_id": "doc_1", "title": "项目周报", "version": 3, "is_current": True},
                {"document_id": "doc_2", "title": "发布复盘", "version": 1, "is_current": False},
            ],
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.task_run_service,
            "update_task_run",
        ) as update_task_run, patch.object(
            self.service.task_run_service,
            "create_confirmation",
            return_value=confirmation,
        ) as create_confirmation, patch.object(
            self.service.task_run_service,
            "merge_task_run_metadata",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace_request, patch.object(
            self.service.llm_service,
            "resolve_doc_request",
        ) as resolve_doc_request, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "需要确认目标文档",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            result = self.service._handle_mentioned_request(message, task_run_id="run_doc_choice")

        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["confirmation_id"], "confirm_doc_choice")
        create_confirmation.assert_called_once()
        self.assertIn("项目周报", create_confirmation.call_args.kwargs["options"][0])
        resolve_workspace_request.assert_not_called()
        resolve_doc_request.assert_not_called()
        update_task_run.assert_any_call(
            "run_doc_choice",
            stage="awaiting_user_confirmation",
            status="waiting_confirmation",
        )

    def test_ambiguous_route_can_be_rescued_by_lightweight_dag_plan(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_ambiguous_dag",
            text="帮我整理一下，给评委看",
            chat_id="c1",
            chat_type="group",
        )
        dag_result = {
            "operation": "create",
            "object": "slides",
            "confidence": 0.74,
            "reason": "Audience-facing material is best handled as slides.",
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "generate_slides", "title": "Prepare presentation"},
                ],
            },
        }
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "plan_workspace_request",
            return_value=dag_result,
        ) as plan_workspace, patch.object(
            self.service.llm_service,
            "route_workspace_request",
            return_value={
                "route": "unknown",
                "confidence": 0.35,
                "needs_clarification": True,
                "reason": "needs a DAG plan",
            },
        ) as route_workspace, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace, patch.object(
            self.service.slides_execution,
            "prepare_slides_execution",
            return_value={
                "reply_preview": "[slides] ready",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package"}],
                "close_title": "slides",
            },
        ) as prepare_slides, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "slides",
                "analysis": None,
                "reply_preview": "done",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [{"artifact_type": "slides_package"}],
            },
        ):
            result = self.service._handle_mentioned_request(message)

        self.assertEqual(result["mode"], "slides")
        route_workspace.assert_called_once_with(message.text)
        plan_workspace.assert_called_once_with("[workspace]", message.text)
        resolve_workspace.assert_not_called()
        prepare_slides.assert_called_once()

    def test_compound_rule_route_uses_lightweight_dag_plan(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_compound_rule_dag",
            text="make slides and canvas",
            chat_id="c1",
            chat_type="group",
        )
        rule_decision = RouteDecision(
            route="slides",
            source="rule",
            confidence=0.96,
            requested_outputs=("slides", "canvas"),
        )
        dag_result = {
            "operation": "create",
            "object": "slides",
            "confidence": 0.88,
            "requested_outputs": ["slides", "canvas"],
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "generate_slides"},
                    {"id": "step_2", "type": "generate_canvas", "depends_on": ["step_1"]},
                ],
            },
        }
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service,
            "_route_request",
            return_value=rule_decision,
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "plan_workspace_request",
            return_value=dag_result,
        ) as plan_workspace, patch.object(
            self.service.llm_service,
            "route_workspace_request",
            return_value={
                "route": "unknown",
                "confidence": 0.4,
                "needs_clarification": True,
                "reason": "needs a DAG plan",
            },
        ) as route_workspace, patch.object(
            self.service.slides_execution,
            "prepare_slides_execution",
            return_value={
                "reply_preview": "[slides]",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package"}],
                "close_title": "slides",
            },
        ) as prepare_slides, patch.object(
            self.service.canvas_execution,
            "prepare_canvas_execution",
            return_value={
                "reply_preview": "[canvas]",
                "analysis": None,
                "artifacts": [{"artifact_type": "canvas"}],
                "close_title": "canvas",
            },
        ) as prepare_canvas, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "slides",
                "analysis": None,
                "reply_preview": "done",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            result = self.service._handle_mentioned_request(message)

        self.assertEqual(result["mode"], "slides")
        plan_workspace.assert_called_once_with("[workspace]", message.text)
        route_workspace.assert_not_called()
        prepare_slides.assert_called_once()
        prepare_canvas.assert_called_once()

    def test_dag_route_can_infer_outputs_from_plan_steps(self) -> None:
        dag_result = {
            "operation": "analyze",
            "object": "workspace",
            "confidence": 0.72,
            "reason": "Concrete artifact steps were planned.",
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "generate_slides"},
                    {"id": "step_2", "type": "generate_canvas"},
                ],
            },
        }
        decision = self.service._route_decision_from_dag_result(
            dag_result,
            fallback=RouteDecision(route="unknown", source="rule", confidence=0.4),
        )

        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides", "canvas"))
        self.assertEqual(dag_result["operation"], "create")
        self.assertEqual(dag_result["object"], "slides")

    def test_dag_route_prefers_first_requested_output_over_conflicting_object(self) -> None:
        dag_result = {
            "operation": "analyze",
            "object": "doc",
            "confidence": 0.78,
            "requested_outputs": ["slides", "doc"],
            "reason": "User asked for slides first, then a document.",
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "generate_slides"},
                    {"id": "step_2", "type": "sync_doc", "depends_on": ["step_1"]},
                ],
            },
        }
        decision = self.service._route_decision_from_dag_result(
            dag_result,
            fallback=RouteDecision(route="unknown", source="rule", confidence=0.4),
        )

        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides", "doc"))
        self.assertEqual(dag_result["operation"], "create")
        self.assertEqual(dag_result["object"], "slides")

    def test_unmatched_request_uses_lightweight_dag_planner_before_execution(self) -> None:
        message = SimpleNamespace(
            session_id="s1",
            message_id="m_dag",
            text="把讨论结果做成给评委看的材料，顺便画个图",
            chat_id="c1",
            chat_type="group",
        )
        dag_result = {
            "operation": "create",
            "object": "slides",
            "route": "slides",
            "confidence": 0.86,
            "reason": "用户需要汇报材料并补充图示",
            "requested_outputs": ["slides", "canvas"],
            "plan": {
                "goal": "生成汇报材料和图示",
                "steps": [
                    {"id": "step_1", "type": "generate_slides", "title": "生成汇报材料", "depends_on": []},
                    {"id": "step_2", "type": "generate_canvas", "title": "生成图示", "depends_on": ["step_1"]},
                ],
            },
        }
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "plan_workspace_request",
            return_value=dag_result,
        ) as plan_workspace, patch.object(
            self.service.llm_service,
            "route_workspace_request",
        ) as route_workspace, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace, patch.object(
            self.service.slides_execution,
            "prepare_slides_execution",
            return_value={
                "reply_preview": "【演示稿】已生成",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package"}],
                "close_title": "slides",
            },
        ) as prepare_slides, patch.object(
            self.service.canvas_execution,
            "prepare_canvas_execution",
            return_value={
                "reply_preview": "【Canvas】已生成",
                "analysis": None,
                "artifacts": [{"artifact_type": "canvas"}],
                "close_title": "canvas",
            },
        ) as prepare_canvas, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "slides",
                "analysis": None,
                "reply_preview": "done",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [{"artifact_type": "slides_package"}, {"artifact_type": "canvas"}],
            },
        ):
            result = self.service._handle_mentioned_request(message)

        self.assertEqual(result["mode"], "slides")
        route_workspace.assert_called_once_with(message.text)
        plan_workspace.assert_called_once_with("[workspace]", message.text)
        resolve_workspace.assert_not_called()
        prepare_slides.assert_called_once()
        prepare_canvas.assert_called_once()

    def test_doc_revision_with_explicit_document_title_uses_matched_target_document(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "s1",
                "message_id": "m1",
                "text": "please update the Release Review risk section",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()
        matched_document = {"document_id": "doc_2", "title": "Release Review", "version": 1}
        with patch.object(
            self.service.memory_service,
            "get_active_episode",
            return_value=None,
        ), patch.object(
            self.service,
            "_build_workspace_context_for_message",
            return_value="[workspace]",
        ), patch.object(
            self.service,
            "_resolve_target_document_for_instruction",
            return_value=matched_document,
        ), patch.object(
            self.service,
            "_route_request",
            return_value=RouteDecision(route="doc", source="rule", confidence=0.98),
        ), patch.object(
            self.service.session_document_service,
            "list_documents",
            return_value=[
                {"document_id": "doc_1", "title": "Project Weekly", "version": 3, "is_current": True},
                matched_document,
            ],
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service,
            "_resolve_llm_result_for_route",
            return_value={"intent": "doc", "reason": "doc route", "doc": {"title": "Release Review", "sections": []}},
        ), patch.object(
            self.service.execution_runner,
            "execute_llm_request",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "ok",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ) as execute_llm_request, patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.task_run_service,
            "update_task_run",
        ):
            result = self.service._handle_mentioned_request(message, task_run_id="run_doc_target")

        self.assertEqual(result["mode"], "doc")
        self.assertIs(execute_llm_request.call_args.kwargs["target_document"], matched_document)

    def test_resolve_target_document_supports_relative_references(self) -> None:
        documents = [
            {"document_id": "doc_1", "title": "Project Weekly", "version": 3, "is_current": True},
            {"document_id": "doc_2", "title": "Release Review", "version": 2, "is_current": False},
            {"document_id": "doc_3", "title": "Risk Memo", "version": 1, "is_current": False},
        ]
        with patch.object(
            self.service.session_document_service,
            "list_documents",
            return_value=documents,
        ):
            current_doc = self.service._resolve_target_document_for_instruction("s1", "请继续修改当前这份文档")
            previous_doc = self.service._resolve_target_document_for_instruction("s1", "请把上一份也更新一下")
            ordinal_doc = self.service._resolve_target_document_for_instruction("s1", "请修一下第3份文档")
            version_doc = self.service._resolve_target_document_for_instruction("s1", "请更新 v2 那份")

        self.assertIs(current_doc, documents[0])
        self.assertIs(previous_doc, documents[1])
        self.assertIs(ordinal_doc, documents[2])
        self.assertIs(version_doc, documents[1])

    def test_fallback_plan_for_doc_can_chain_slides(self) -> None:
        plan = self.service.execution_planner.resolve_execution_plan(
            intent="doc",
            reason="需要先沉淀文档，再给出演示材料",
            llm_result={
                "slides": {
                    "theme": "报名汇报",
                    "slides": [{"title": "背景", "bullets": ["目标"]}],
                }
            },
            instruction="帮我整理成文档，并顺手出一个汇报PPT大纲",
        )

        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_slides"])

    def test_doc_route_requires_doc_step_before_optional_slides(self) -> None:
        plan = self.service.execution_planner.resolve_execution_plan(
            intent="slides",
            reason="planner missed the required document step",
            llm_result={
                "operation": "create",
                "object": "doc",
                "intent": "slides",
                "slides": {
                    "theme": "report",
                    "slides": [{"title": "背景", "bullets": ["目标"]}],
                },
                "plan": {
                    "steps": [
                        {
                            "id": "step_1",
                            "type": "generate_slides",
                            "title": "generate slides only",
                        }
                    ]
                },
            },
            instruction="write the discussion into a document",
        )

        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_slides"])

    def test_doc_protocol_keeps_sync_doc_and_adds_requested_slides_step(self) -> None:
        plan = self.service.execution_planner.resolve_execution_plan(
            intent="doc",
            reason="专项文档 prompt 只返回了文档计划",
            llm_result={
                "operation": "create",
                "object": "doc",
                "route": "doc",
                "requested_outputs": ["doc", "slides"],
                "plan": {
                    "steps": [
                        {
                            "id": "step_1",
                            "type": "sync_doc",
                            "title": "同步文档",
                        }
                    ]
                },
            },
            instruction="帮我整理成文档并生成PPT",
        )

        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_slides"])
        self.assertEqual(plan.steps[1].depends_on, ["step_1"])

    def test_doc_response_package_keeps_doc_content_independent_from_requested_slides(self) -> None:
        analysis_response = AnalyzeResponse(
            session_id="doc_requested_outputs_session",
            summary="Discussion summary",
            tasks=[],
            risks=[],
            next_actions=["Next action"],
            agent_traces=[],
        )
        with patch.object(self.service.doc_execution, "resolve_doc_stats_as_of", return_value=None), patch.object(
            self.service.memory_service,
            "build_discussion_block",
            return_value="Discussion context",
        ), patch.object(
            self.service,
            "_build_analysis_from_llm",
            return_value=analysis_response,
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ) as save_round, patch.object(
            self.service.llm_service,
            "generate_presentation_package",
        ) as generate_slides:
            package, analysis = self.service.doc_execution.build_doc_response_package(
                session_id="doc_requested_outputs_session",
                instruction="write this into a collaboration document",
                llm_result={
                    "requested_outputs": ["doc", "slides"],
                    "slides": {
                        "theme": "Launch Review",
                        "audience": "Team",
                        "slides": [
                            {"title": "Context", "bullets": ["Goal"]},
                        ],
                    },
                },
                workspace_context="Discussion context",
                episode_id=None,
                reason="test",
                source_message_id=None,
            )

        self.assertEqual(analysis, analysis_response)
        self.assertTrue(any(section["paragraphs"] == ["Discussion summary"] for section in package["sections"]))
        self.assertFalse(any(section["heading"].startswith("P1.") for section in package["sections"]))
        save_round.assert_called_once()
        generate_slides.assert_not_called()

    def test_doc_instruction_overrides_summary_protocol(self) -> None:
        llm_result = {
            "operation": "analyze",
            "object": "summary",
            "reason": "误判成普通总结",
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "analyze_discussion", "title": "总结讨论"},
                    {"id": "step_2", "type": "sync_doc", "title": "同步到文档"},
                ]
            },
        }

        plan = self.service.execution_planner.resolve_execution_plan(
            intent="summary",
            reason="误判成普通总结",
            llm_result=llm_result,
            instruction="你再来总结成文档",
        )

        self.assertEqual(llm_result["operation"], "create")
        self.assertEqual(llm_result["object"], "doc")
        self.assertEqual(llm_result["route"], "doc")
        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc"])

    def test_plain_doc_instruction_uses_doc_fallback_plan(self) -> None:
        plan = self.service.execution_planner.resolve_execution_plan(
            intent="summary",
            reason="误判成总结",
            llm_result={"operation": "analyze", "object": "summary"},
            instruction="把刚才的内容写成文档",
        )

        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc"])

    def test_operation_object_protocol_derives_route_without_legacy_intent(self) -> None:
        protocol = self.service.execution_planner.normalize_request_protocol(
            {
                "operation": "read",
                "object": "tasks",
                "reason": "read current task snapshot",
            }
        )

        self.assertEqual(protocol.operation, "read")
        self.assertEqual(protocol.object, "tasks")
        self.assertEqual(protocol.route, "status")

    def test_route_decision_overrides_deep_llm_protocol(self) -> None:
        llm_result = {
            "operation": "analyze",
            "object": "summary",
            "reason": "深度模型误判成总结",
        }

        self.service._apply_route_decision_to_llm_result(
            llm_result,
            RouteDecision(route="doc", source="rule", confidence=0.98, reason="文档规则命中"),
        )
        protocol = self.service.execution_planner.normalize_request_protocol(llm_result)

        self.assertEqual(protocol.operation, "create")
        self.assertEqual(protocol.object, "doc")
        self.assertEqual(protocol.route, "doc")

    def test_route_decision_preserves_artifact_mutation_operation(self) -> None:
        llm_result = {
            "operation": "delete",
            "object": "doc",
            "reason": "remove a document section",
        }

        self.service._apply_route_decision_to_llm_result(
            llm_result,
            RouteDecision(route="doc", source="rule", confidence=0.98, reason="doc rule matched"),
        )
        protocol = self.service.execution_planner.normalize_request_protocol(llm_result)

        self.assertEqual(protocol.operation, "update")
        self.assertEqual(protocol.object, "doc")
        self.assertEqual(protocol.route, "doc")

    def test_doc_route_uses_specialized_doc_resolver(self) -> None:
        with patch.object(
            self.service.llm_service,
            "resolve_doc_request",
            return_value={"doc": {"title": "文档", "sections": []}},
        ) as resolve_doc, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace:
            result = self.service._resolve_llm_result_for_route(
                RouteDecision(route="doc", source="rule", confidence=0.98),
                "[workspace]",
                "总结成文档",
            )

        self.assertEqual(result["doc"]["title"], "文档")
        resolve_doc.assert_called_once_with("[workspace]", "总结成文档")
        resolve_workspace.assert_not_called()

    def test_analysis_route_uses_specialized_analysis_resolver(self) -> None:
        with patch.object(
            self.service.llm_service,
            "resolve_analysis_request",
            return_value={"summary": "摘要"},
        ) as resolve_analysis, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace:
            result = self.service._resolve_llm_result_for_route(
                RouteDecision(route="tasks", source="rule", confidence=0.9),
                "[workspace]",
                "整理任务清单",
            )

        self.assertEqual(result["summary"], "摘要")
        resolve_analysis.assert_called_once_with("[workspace]", "整理任务清单", "tasks")
        resolve_workspace.assert_not_called()

    def test_status_route_skips_deep_llm_resolver(self) -> None:
        with patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace:
            result = self.service._resolve_llm_result_for_route(
                RouteDecision(route="status", source="rule", confidence=0.95),
                "[workspace]",
                "查看任务列表",
            )

        self.assertEqual(result["operation"], "read")
        self.assertEqual(result["object"], "tasks")
        self.assertEqual(result["route"], "status")
        self.assertEqual(result["plan"]["steps"][0]["type"], "answer_status")
        resolve_workspace.assert_not_called()

    def test_slides_route_skips_workspace_resolver(self) -> None:
        with patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace:
            result = self.service._resolve_llm_result_for_route(
                RouteDecision(route="slides", source="rule", confidence=0.96),
                "[workspace]",
                "生成演示提纲",
            )

        self.assertEqual(result["operation"], "create")
        self.assertEqual(result["object"], "slides")
        self.assertEqual(result["route"], "slides")
        self.assertEqual(result["plan"]["steps"][0]["type"], "generate_slides")
        resolve_workspace.assert_not_called()

    def test_prepare_slides_execution_persists_local_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.service.presentation_artifact_service = PresentationArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(session_id="s1", message_id="m1", text="生成演示稿", chat_id=None)

            result = self.service.slides_execution.prepare_slides_execution(
                message,
                llm_result={
                    "slides": {
                        "theme": "报名汇报",
                        "slides": [{"title": "背景", "bullets": ["目标"]}],
                    }
                },
                workspace_context="[workspace]",
                task_run_id="run_slides",
            )

            artifact = result["artifacts"][0]
            self.assertEqual(artifact["provider"], "llm")
            self.assertEqual(artifact["url"], "/api/artifacts/slides/报名汇报-run_slides.html")
            self.assertTrue((Path(tmpdir) / "报名汇报-run_slides.html").is_file())
            self.assertTrue((Path(tmpdir) / "报名汇报-run_slides.pptx").is_file())
            self.assertIn("speaker_notes", artifact["preview"]["slides"][0])
            self.assertIn("预览链接：", result["reply_preview"])
            self.assertIn("PPT 下载：", result["reply_preview"])
            self.assertIn("报名汇报-run_slides.pptx", result["reply_preview"])

    def test_prepare_slides_execution_marks_template_fallback_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            self.service.llm_service,
            "generate_presentation_package",
            side_effect=RuntimeError("llm unavailable"),
        ):
            self.service.presentation_artifact_service = PresentationArtifactService(root_dir=Path(tmpdir))
            message = SimpleNamespace(session_id="s1", message_id="m1", text="生成演示稿", chat_id=None)

            result = self.service.slides_execution.prepare_slides_execution(
                message,
                llm_result={},
                workspace_context="[workspace]",
                task_run_id="run_slides",
            )

        artifact = result["artifacts"][0]
        self.assertEqual(artifact["provider"], "fallback")
        self.assertEqual(artifact["url"], "/api/artifacts/slides/汇报演示稿-run_slides.html")

    def test_memory_gate_runs_only_for_historical_requests(self) -> None:
        self.assertFalse(
            self.service._should_run_memory_gate(
                RouteDecision(route="doc", source="rule", confidence=0.98),
                "总结成文档",
            )
        )
        self.assertFalse(
            self.service._should_run_memory_gate(
                RouteDecision(route="status", source="rule", confidence=0.95),
                "查看任务列表",
            )
        )
        self.assertTrue(
            self.service._should_run_memory_gate(
                RouteDecision(route="summary", source="rule", confidence=0.82),
                "对比一下上次和这次的变化",
            )
        )

    def test_read_risks_can_answer_from_payload_without_task_snapshot(self) -> None:
        reply = self.service.response_formatter.format_status_reply("当前有什么风险", [], {"risks": ["接口联调时间紧"]})

        self.assertIn("当前风险", reply)
        self.assertIn("接口联调时间紧", reply)

    def test_read_task_route_forces_answer_status_plan(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "查看任务列表", "chat_id": "c1", "chat_type": "p2p"},
        )()
        with patch.object(
            self.service.status_execution,
            "prepare_status_execution",
            return_value={
                "reply_preview": "【当前协作状态】",
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            },
        ) as prepare_status, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "status",
                "analysis": None,
                "reply_preview": "【当前协作状态】",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            result = self.service.execution_runner.execute_llm_request(
                message,
                {
                    "operation": "read",
                    "object": "tasks",
                    "intent": "tasks",
                    "reason": "模型误判成整理任务",
                    "tasks": [],
                    "plan": {
                        "steps": [
                            {
                                "id": "step_1",
                                "type": "analyze_discussion",
                                "title": "整理任务",
                            }
                        ]
                    },
                },
                "[workspace]",
                None,
            )

        self.assertEqual(result["mode"], "status")
        prepare_status.assert_called_once()

    def test_analyze_task_route_rejects_read_only_plan(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "organize todos", "chat_id": "c1", "chat_type": "group"},
        )()
        with patch.object(
            self.service.analysis_execution,
            "prepare_analysis_execution",
            return_value={
                "reply_preview": "[tasks]",
                "analysis": None,
                "artifacts": [],
                "close_title": "tasks",
            },
        ) as prepare_analysis, patch.object(
            self.service.status_execution,
            "prepare_status_execution",
            return_value={
                "reply_preview": "should not run",
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            },
        ) as prepare_status, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "tasks",
                "analysis": None,
                "reply_preview": "[tasks]",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            result = self.service.execution_runner.execute_llm_request(
                message,
                {
                    "operation": "analyze",
                    "object": "tasks",
                    "intent": "status",
                    "reason": "planner picked the wrong read-only step",
                    "plan": {
                        "steps": [
                            {
                                "id": "step_1",
                                "type": "answer_status",
                                "title": "answer status",
                            }
                        ]
                    },
                },
                "[workspace]",
                None,
            )

        self.assertEqual(result["mode"], "tasks")
        prepare_analysis.assert_called_once()
        prepare_status.assert_not_called()

    def test_execute_llm_request_runs_planner_steps_in_order(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "帮我整理成文档并生成PPT", "chat_id": "c1"},
        )()
        with patch.object(
            self.service.doc_execution,
            "prepare_doc_execution",
            return_value={
                "reply_preview": "【文档同步】\n已生成文档",
                "analysis": None,
                "artifacts": [{"artifact_type": "document", "title": "文档"}],
                "close_title": "文档",
            },
        ) as prepare_doc, patch.object(
            self.service.slides_execution,
            "prepare_slides_execution",
            return_value={
                "reply_preview": "【演示稿】\n已生成演示稿大纲",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package", "title": "演示稿"}],
                "close_title": "slides",
            },
        ) as prepare_slides, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": 1,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "combined",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ) as deliver_reply, patch.object(
            self.service.memory_service,
            "close_active_episode",
        ):
            result = self.service.execution_runner.execute_llm_request(
                message,
                {
                    "intent": "doc",
                    "reason": "用户要文档和演示稿",
                    "slides": {
                        "theme": "报名汇报",
                        "slides": [{"title": "背景", "bullets": ["目标"]}],
                    },
                },
                "[workspace]",
                1,
                task_run_id=None,
            )

        self.assertEqual(result["mode"], "doc")
        prepare_doc.assert_called_once()
        prepare_slides.assert_called_once()
        deliver_reply.assert_called_once()
        combined_reply = deliver_reply.call_args.args[2]
        self.assertIn("【文档同步】", combined_reply)
        self.assertIn("【演示稿】", combined_reply)
        artifact_types = [item["artifact_type"] for item in deliver_reply.call_args.kwargs["artifacts"]]
        self.assertIn("agent_plan", artifact_types)
        self.assertIn("document", artifact_types)
        self.assertIn("slides_package", artifact_types)

    def test_execute_llm_request_runs_dag_steps_by_dependency_order(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "先生成PPT，再画流程图", "chat_id": "c1"},
        )()
        calls: list[str] = []

        def slides_result(*args, **kwargs):
            calls.append("slides")
            return {
                "reply_preview": "[slides]",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package"}],
                "close_title": "slides",
            }

        def canvas_result(*args, **kwargs):
            calls.append("canvas")
            return {
                "reply_preview": "[canvas]",
                "analysis": None,
                "artifacts": [{"artifact_type": "canvas"}],
                "close_title": "canvas",
            }

        with patch.object(
            self.service.slides_execution,
            "prepare_slides_execution",
            side_effect=slides_result,
        ), patch.object(
            self.service.canvas_execution,
            "prepare_canvas_execution",
            side_effect=canvas_result,
        ), patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "slides",
                "analysis": None,
                "reply_preview": "combined",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            self.service.execution_runner.execute_llm_request(
                message,
                {
                    "operation": "create",
                    "object": "slides",
                    "requested_outputs": ["slides", "canvas"],
                    "plan": {
                        "steps": [
                            {"id": "step_2", "type": "generate_canvas", "depends_on": ["step_1"]},
                            {"id": "step_1", "type": "generate_slides"},
                        ]
                    },
                },
                "[workspace]",
                None,
                task_run_id=None,
            )

        self.assertEqual(calls, ["slides", "canvas"])

    def test_execute_llm_request_reuses_slides_package_across_doc_and_slides(self) -> None:
        package = {
            "title": "Review Deck",
            "theme": "review",
            "audience": "team",
            "slides": [
                {"title": "Context", "bullets": ["Goal"]},
                {"title": "Next Steps", "bullets": ["Ship"]},
            ],
        }
        llm_result = {
            "operation": "create",
            "object": "doc",
            "requested_outputs": ["doc", "slides"],
            "plan": {
                "steps": [
                    {"id": "step_1", "type": "sync_doc"},
                    {"id": "step_2", "type": "generate_slides", "depends_on": ["step_1"]},
                ],
            },
        }
        message = SimpleNamespace(session_id="s1", message_id="m_reuse", text="make doc and slides", chat_id="c1")
        analysis_response = AnalyzeResponse(
            session_id="s1",
            summary="Discussion summary",
            tasks=[],
            risks=[],
            next_actions=[],
            agent_traces=[],
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            self.service.llm_service,
            "generate_presentation_package",
            return_value=package,
        ) as generate_package, patch.object(
            self.service.doc_execution,
            "resolve_doc_stats_as_of",
            return_value=None,
        ), patch.object(
            self.service.memory_service,
            "build_discussion_block",
            return_value="[workspace]",
        ), patch.object(
            self.service,
            "_build_analysis_from_llm",
            return_value=analysis_response,
        ), patch.object(
            self.service.memory_service,
            "save_round",
        ), patch.object(
            self.service.doc_execution,
            "sync_package_to_session_doc",
            return_value=DocumentSyncResult(
                mode="created",
                status="ready",
                summary_lines=["- Document ready"],
                document_info={"document_id": "doc_1", "url": "https://example.test/doc_1"},
            ),
        ), patch.object(
            self.service.doc_execution,
            "build_document_artifact",
            return_value={"artifact_type": "document", "title": "Document"},
        ), patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "episode_id": None,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "combined",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ):
            self.service.presentation_artifact_service = PresentationArtifactService(root_dir=Path(tmpdir))
            self.service.execution_runner.execute_llm_request(
                message,
                llm_result,
                "[workspace]",
                None,
                task_run_id=None,
            )

        generate_package.assert_called_once_with("[workspace]", message.text)
        self.assertIs(llm_result["slides"], package)
        self.assertEqual(llm_result["_slides_provider"], "llm")

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

    def test_handle_message_replies_with_transcription_notice(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "s1",
                "message_id": "m2",
                "sender_id": "u1",
                "sender_user_id": "user_1",
                "sender_open_id": "open_1",
                "sender_union_id": "union_1",
                "text": "",
                "raw_text": "",
                "chat_id": "c1",
                "chat_type": "p2p",
                "message_type": "audio",
                "is_mentioned": False,
                "mentioned_users": [],
                "event_id": "evt_2",
                "transcription_notice": "这条语音消息处理失败了，请直接发送文本消息。",
            },
        )()
        with patch.object(self.service, "_ensure_sender_alias"), patch.object(
            self.service.memory_service,
            "save_user_message",
        ) as save_user_message, patch.object(
            self.service.task_run_service,
            "create_task_run",
            return_value=SimpleNamespace(task_run_id="run_456"),
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "update_task_run",
        ) as update_task_run, patch.object(
            self.service.reply_sender,
            "deliver_reply",
            return_value={
                "session_id": "s1",
                "mode": "speech_notice",
                "analysis": None,
                "reply_preview": "这条语音消息处理失败了，请直接发送文本消息。",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ) as deliver_reply:
            result = self.service.handle_message(message)

        self.assertEqual(result["task_run_id"], "run_456")
        deliver_reply.assert_called_once()
        self.assertEqual(save_user_message.call_args.kwargs["content"], "[语音消息]")
        last_update_call = update_task_run.call_args_list[-1]
        self.assertEqual(last_update_call.kwargs["status"], "completed")
        self.assertEqual(last_update_call.kwargs["stage"], "delivered")
        self.assertEqual(upsert_step.call_args_list[-1].kwargs["status"], "done")

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
            self.service,
            "_resolve_llm_result_for_route",
            return_value={"intent": "doc", "reason": "???????"},
        ) as resolve_llm_result_for_route, patch.object(
            self.service.execution_runner,
            "execute_llm_request",
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
        resolve_llm_result_for_route.assert_called_once()
        resumed_instruction = resolve_llm_result_for_route.call_args.args[2]
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

    def test_resume_after_confirmation_for_doc_selection_uses_target_document(self) -> None:
        selected_doc = {"document_id": "doc_2", "title": "发布复盘", "version": 2}
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
                    "instruction": "请继续修文档",
                    "workspace_context": "[workspace]",
                    "active_episode_id": 7,
                    "question": "你要更新哪一份文档？",
                    "reason": "当前会话里有多份协作文档",
                    "options": ["项目周报 (v3, 当前)", "发布复盘 (v2)"],
                    "confirmation_id": "confirm_doc_1",
                }
            },
        ), patch.object(
            self.service.session_document_service,
            "list_documents",
            return_value=[
                {"document_id": "doc_1", "title": "项目周报", "version": 3, "is_current": True},
                selected_doc,
            ],
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.task_run_service,
            "update_task_run",
        ), patch.object(
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
            self.service,
            "_resolve_llm_result_for_route",
            return_value={"intent": "doc", "reason": "doc schema", "doc": {"title": "发布复盘", "sections": []}},
        ), patch.object(
            self.service.execution_runner,
            "execute_llm_request",
            return_value={
                "session_id": "s1",
                "episode_id": 7,
                "mode": "doc",
                "analysis": None,
                "reply_preview": "继续处理",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            },
        ) as execute_llm_request:
            result = self.service.resume_task_run_after_confirmation(
                "run_123",
                confirmation_id="confirm_doc_1",
                answer_value="发布复盘 (v2)",
                answered_by="tester",
            )

        self.assertIsNotNone(result)
        self.assertIs(execute_llm_request.call_args.kwargs["target_document"], selected_doc)

    def test_revise_document_from_task_run_creates_workbench_run(self) -> None:
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source")
        revision_detail = SimpleNamespace(task_run_id="run_revision")
        final_detail = SimpleNamespace(task_run_id="run_revision", session_id="s1")
        with patch.object(
            self.service.task_run_service,
            "get_task_run",
            side_effect=[source_detail, final_detail],
        ), patch.object(
            self.service.task_run_service,
            "create_task_run",
            return_value=revision_detail,
        ) as create_task_run, patch.object(
            self.service.task_run_service,
            "upsert_step",
        ) as upsert_step, patch.object(
            self.service.task_run_service,
            "update_task_run",
        ), patch.object(
            self.service.memory_service,
            "save_user_message",
        ) as save_user_message, patch.object(
            self.service.memory_service,
            "build_workspace_context",
            return_value="[workspace]",
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=False,
        ), patch.object(
            self.service.doc_execution,
            "prepare_doc_execution",
            return_value={
                "reply_preview": "【文档同步】\n已修订",
                "analysis": None,
                "artifacts": [{"artifact_type": "document", "title": "协作文档"}],
            },
        ) as prepare_doc, patch.object(
            self.service.result_persistence,
            "persist_task_run_result",
        ) as persist_result:
            result = self.service.revise_document_from_task_run(
                "run_source",
                instruction="补充风险部分",
                requested_by="tester",
            )

        self.assertIs(result, final_detail)
        create_task_run.assert_called_once()
        self.assertEqual(create_task_run.call_args.kwargs["source_type"], "workbench")
        self.assertEqual(create_task_run.call_args.kwargs["source_ref"], "run_source")
        self.assertEqual(create_task_run.call_args.kwargs["intent"], "doc")
        save_user_message.assert_called_once()
        self.assertEqual(save_user_message.call_args.kwargs["content"], "补充风险部分")
        prepare_doc.assert_called_once()
        persist_result.assert_called_once()
        self.assertEqual(persist_result.call_args.args[0], "run_revision")
        upsert_step.assert_any_call(
            "run_revision",
            step_key="request_received",
            title="接收文档修订指令",
            step_type="input",
            status="done",
            input_payload={
                "source_task_run_id": "run_source",
                "instruction": "补充风险部分",
                "requested_by": "tester",
                "document_id": None,
            },
        )

    def test_revise_document_from_task_run_uses_doc_resolver(self) -> None:
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source")
        revision_detail = SimpleNamespace(task_run_id="run_revision")
        final_detail = SimpleNamespace(task_run_id="run_revision", session_id="s1")
        with patch.object(
            self.service.task_run_service,
            "get_task_run",
            side_effect=[source_detail, final_detail],
        ), patch.object(
            self.service.task_run_service,
            "create_task_run",
            return_value=revision_detail,
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.task_run_service,
            "update_task_run",
        ), patch.object(
            self.service.memory_service,
            "save_user_message",
        ), patch.object(
            self.service.memory_service,
            "build_workspace_context",
            return_value="[workspace]",
        ), patch.object(
            self.service.session_document_service,
            "get_current_document",
            return_value={"title": "Current Doc", "version": 2},
        ), patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "resolve_doc_request",
            return_value={"doc": {"title": "Current Doc", "sections": []}, "reason": "doc schema"},
        ) as resolve_doc_request, patch.object(
            self.service.llm_service,
            "resolve_workspace_request",
        ) as resolve_workspace_request, patch.object(
            self.service.doc_execution,
            "prepare_doc_execution",
            return_value={
                "reply_preview": "updated",
                "analysis": None,
                "artifacts": [],
            },
        ) as prepare_doc, patch.object(
            self.service.result_persistence,
            "persist_task_run_result",
        ):
            result = self.service.revise_document_from_task_run(
                "run_source",
                instruction="add risk section",
                requested_by="tester",
            )

        self.assertIs(result, final_detail)
        resolve_doc_request.assert_called_once()
        self.assertEqual(resolve_doc_request.call_args.args[0], "[workspace]")
        self.assertIn("add risk section", resolve_doc_request.call_args.args[1])
        resolve_workspace_request.assert_not_called()
        llm_result = prepare_doc.call_args.kwargs["llm_result"]
        self.assertEqual(llm_result["operation"], "update")
        self.assertEqual(llm_result["object"], "doc")
        self.assertEqual(llm_result["route"], "doc")

    def test_revise_document_from_task_run_can_target_specific_document_id(self) -> None:
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source")
        revision_detail = SimpleNamespace(task_run_id="run_revision")
        final_detail = SimpleNamespace(task_run_id="run_revision", session_id="s1")
        selected_doc = {"document_id": "doc_target", "title": "Target Doc", "version": 3}
        with patch.object(
            self.service.task_run_service,
            "get_task_run",
            side_effect=[source_detail, final_detail],
        ), patch.object(
            self.service.task_run_service,
            "create_task_run",
            return_value=revision_detail,
        ), patch.object(
            self.service.task_run_service,
            "upsert_step",
        ), patch.object(
            self.service.task_run_service,
            "update_task_run",
        ), patch.object(
            self.service.memory_service,
            "save_user_message",
        ), patch.object(
            self.service.memory_service,
            "build_workspace_context",
            return_value="[workspace]",
        ), patch.object(
            self.service.session_document_service,
            "get_document",
            return_value=selected_doc,
        ) as get_document, patch.object(
            self.service.llm_service,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.service.llm_service,
            "resolve_doc_request",
            return_value={"doc": {"title": "Target Doc", "sections": []}, "reason": "doc schema"},
        ), patch.object(
            self.service.doc_execution,
            "prepare_doc_execution",
            return_value={
                "reply_preview": "updated",
                "analysis": None,
                "artifacts": [],
            },
        ) as prepare_doc, patch.object(
            self.service.result_persistence,
            "persist_task_run_result",
        ):
            result = self.service.revise_document_from_task_run(
                "run_source",
                instruction="rename this section",
                requested_by="tester",
                document_id="doc_target",
            )

        self.assertIs(result, final_detail)
        get_document.assert_called_once_with("s1", "doc_target")
        self.assertIs(prepare_doc.call_args.kwargs["target_document"], selected_doc)

    def test_revise_slides_from_task_run_creates_workbench_run(self) -> None:
        current_package = {
            "theme": "报名汇报",
            "version": 1,
            "slides": [
                {
                    "title": "背景",
                    "bullets": ["目标"],
                    "speaker_notes": "旧讲稿",
                    "duration_sec": 45,
                }
            ],
        }
        slides_artifact = SimpleNamespace(
            artifact_id="artifact_slides",
            artifact_type="slides_package",
            preview_json=json.dumps(current_package, ensure_ascii=False),
        )
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source", artifacts=[slides_artifact])
        revision_detail = SimpleNamespace(task_run_id="run_slides_revision")
        final_detail = SimpleNamespace(task_run_id="run_slides_revision", session_id="s1")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.service.presentation_artifact_service = PresentationArtifactService(root_dir=Path(tmpdir))
            with patch.object(
                self.service.task_run_service,
                "get_task_run",
                side_effect=[source_detail, final_detail],
            ), patch.object(
                self.service.task_run_service,
                "create_task_run",
                return_value=revision_detail,
            ) as create_task_run, patch.object(
                self.service.task_run_service,
                "upsert_step",
            ) as upsert_step, patch.object(
                self.service.task_run_service,
                "update_task_run",
            ), patch.object(
                self.service.memory_service,
                "save_user_message",
            ) as save_user_message, patch.object(
                self.service.memory_service,
                "build_workspace_context",
                return_value="[workspace]",
            ), patch.object(
                self.service.llm_service,
                "is_configured",
                return_value=False,
            ), patch.object(
                self.service.result_persistence,
                "persist_task_run_result",
            ) as persist_result:
                result = self.service.revise_slides_from_task_run(
                    "run_source",
                    instruction="把第 1 页改成评委视角",
                    requested_by="tester",
                    artifact_id="artifact_slides",
                )

            self.assertTrue((Path(tmpdir) / "报名汇报-run_slides_revision.html").is_file())

        self.assertIs(result, final_detail)
        create_task_run.assert_called_once()
        self.assertEqual(create_task_run.call_args.kwargs["source_type"], "workbench")
        self.assertEqual(create_task_run.call_args.kwargs["source_ref"], "run_source")
        self.assertEqual(create_task_run.call_args.kwargs["intent"], "slides")
        save_user_message.assert_called_once()
        self.assertEqual(save_user_message.call_args.kwargs["content"], "把第 1 页改成评委视角")
        persist_result.assert_called_once()
        self.assertEqual(persist_result.call_args.args[0], "run_slides_revision")
        result_payload = persist_result.call_args.kwargs["result"]
        artifact = result_payload["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "slides_package")
        self.assertEqual(artifact["provider"], "local")
        self.assertEqual(artifact["url"], "/api/artifacts/slides/报名汇报-run_slides_revision.html")
        self.assertEqual(artifact["preview"]["version"], 2)
        self.assertIn("把第 1 页改成评委视角", artifact["preview"]["slides"][0]["speaker_notes"])
        upsert_step.assert_any_call(
            "run_slides_revision",
            step_key="request_received",
            title="接收演示稿修订指令",
            step_type="input",
            status="done",
            input_payload={
                "source_task_run_id": "run_source",
                "source_artifact_id": "artifact_slides",
                "instruction": "把第 1 页改成评委视角",
                "requested_by": "tester",
            },
        )

    def test_revise_slides_from_task_run_tolerates_non_numeric_source_version(self) -> None:
        current_package = {
            "theme": "报名汇报",
            "slides": [{"title": "背景", "bullets": ["目标"]}],
        }
        slides_artifact = SimpleNamespace(
            artifact_id="artifact_slides",
            artifact_type="slides_package",
            preview_json=json.dumps(current_package, ensure_ascii=False),
            version="draft",
        )
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source", artifacts=[slides_artifact])
        revision_detail = SimpleNamespace(task_run_id="run_slides_revision")
        final_detail = SimpleNamespace(task_run_id="run_slides_revision", session_id="s1")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.service.presentation_artifact_service = PresentationArtifactService(root_dir=Path(tmpdir))
            with patch.object(
                self.service.task_run_service,
                "get_task_run",
                side_effect=[source_detail, final_detail],
            ), patch.object(
                self.service.task_run_service,
                "create_task_run",
                return_value=revision_detail,
            ), patch.object(
                self.service.task_run_service,
                "upsert_step",
            ), patch.object(
                self.service.task_run_service,
                "update_task_run",
            ), patch.object(
                self.service.memory_service,
                "save_user_message",
            ), patch.object(
                self.service.memory_service,
                "build_workspace_context",
                return_value="[workspace]",
            ), patch.object(
                self.service.llm_service,
                "is_configured",
                return_value=False,
            ), patch.object(
                self.service.result_persistence,
                "persist_task_run_result",
            ) as persist_result:
                self.service.revise_slides_from_task_run(
                    "run_source",
                    instruction="增加评委视角",
                    requested_by="tester",
                    artifact_id="artifact_slides",
                )

        artifact = persist_result.call_args.kwargs["result"]["artifacts"][0]
        self.assertEqual(artifact["preview"]["version"], 2)

    def test_revise_slides_from_task_run_requires_slides_artifact(self) -> None:
        source_detail = SimpleNamespace(session_id="s1", task_run_id="run_source", artifacts=[])
        with patch.object(
            self.service.task_run_service,
            "get_task_run",
            return_value=source_detail,
        ):
            with self.assertRaises(ValueError):
                self.service.revise_slides_from_task_run(
                    "run_source",
                    instruction="把第 1 页讲得更像评委视角",
                    requested_by="tester",
                )

    def test_bundle_delivery_from_task_run_creates_manifest_artifact(self) -> None:
        detail = SimpleNamespace(
            task_run_id="run_delivery",
            session_id="s1",
            source_type="im",
            source_ref="chat_1",
            trigger_message_id="m1",
            title="报名系统汇报",
            status="completed",
            steps=[SimpleNamespace(step_key="plan", status="done")],
            artifacts=[
                SimpleNamespace(
                    artifact_id="artifact_doc",
                    artifact_type="document",
                    title="需求文档",
                    provider="feishu",
                    status="ready",
                    url="https://feishu.example/doc",
                    version=1,
                ),
                SimpleNamespace(
                    artifact_id="artifact_slides",
                    artifact_type="slides_package",
                    title="评审演示稿",
                    provider="local",
                    status="ready",
                    url="/api/artifacts/slides/run_delivery.html",
                    version=1,
                    preview_json=json.dumps(
                        {
                            "slides": [
                                {"title": "目标", "bullets": ["入口", "产物"], "speaker_notes": "讲清楚 IM 入口", "duration_sec": 45},
                                {"title": "验收", "bullets": ["Doc", "PPT", "Canvas"], "speaker_notes": "展示交付闭环", "duration_sec": 60},
                            ],
                            "exports": {
                                "html": "/api/artifacts/slides/run_delivery.html",
                                "pptx": "/api/artifacts/slides/run_delivery.pptx",
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            confirmations=[],
            session_documents=[],
        )
        final_detail = SimpleNamespace(task_run_id="run_delivery", session_id="s1")
        card_calls = []
        fake_message_api = SimpleNamespace(
            send_interactive_message=lambda receive_id, card, receive_id_type="chat_id": card_calls.append(
                {"receive_id": receive_id, "card": card, "receive_id_type": receive_id_type}
            )
            or {"code": 0}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            self.service.delivery_artifact_service.root_dir = Path(tmpdir)
            self.service.message_api = fake_message_api
            with patch.object(
                self.service.task_run_service,
                "get_task_run",
                side_effect=[detail, final_detail],
            ), patch.object(
                self.service.task_run_service,
                "upsert_step",
            ) as upsert_step, patch.object(
                self.service.task_run_service,
                "create_artifact",
            ) as create_artifact, patch.object(
                self.service.task_run_service,
                "update_task_run",
            ) as update_task_run, patch.object(
                settings,
                "feishu_reply_enabled",
                True,
            ), patch.object(
                settings,
                "feishu_reply_card_enabled",
                True,
            ):
                result = self.service.bundle_delivery_from_task_run("run_delivery", requested_by="tester")

            self.assertTrue((Path(tmpdir) / "run_delivery.html").is_file())

        self.assertIs(result, final_detail)
        create_artifact.assert_called_once()
        self.assertEqual(create_artifact.call_args.kwargs["artifact_type"], "delivery_bundle")
        self.assertEqual(create_artifact.call_args.kwargs["url"], "/api/artifacts/delivery/run_delivery.html")
        preview = create_artifact.call_args.kwargs["preview"]
        self.assertEqual(preview["schema"], "agent-pilot.delivery.v1")
        self.assertEqual(len(preview["artifacts"]), 2)
        self.assertGreaterEqual(len(preview["artifact_summaries"]), 2)
        slides_summary = next(item for item in preview["artifact_summaries"] if item["artifact_type"] == "slides_package")
        self.assertIn("2 页", slides_summary["metrics"])
        self.assertTrue(any("讲者备注" in item for item in slides_summary["metrics"]))
        self.assertTrue(preview["highlights"])
        self.assertIn("context_pack", preview)
        self.assertTrue(preview["context_pack"]["used_sources"])
        self.assertEqual({item["key"]: item["status"] for item in preview["checks"]}["document"], "ready")
        self.assertEqual({item["key"]: item["status"] for item in preview["checks"]}["presentation_or_canvas"], "ready")
        self.assertEqual(card_calls[0]["receive_id"], "chat_1")
        self.assertEqual(card_calls[0]["card"]["header"]["title"]["content"], "任务交付包已生成")
        upsert_step.assert_called_once()
        self.assertEqual(upsert_step.call_args.kwargs["step_key"], "delivery_bundle")
        self.assertTrue(upsert_step.call_args.kwargs["output_payload"]["im_card_sent"])
        update_task_run.assert_called_once()
        self.assertEqual(update_task_run.call_args.kwargs["stage"], "delivered")

    def test_bundle_delivery_from_task_run_does_not_complete_running_task(self) -> None:
        detail = SimpleNamespace(
            task_run_id="run_delivery",
            session_id="s1",
            source_type="im",
            source_ref="m1",
            trigger_message_id="m1",
            title="报名系统汇报",
            status="running",
            steps=[SimpleNamespace(step_key="plan", status="done")],
            artifacts=[
                SimpleNamespace(
                    artifact_id="artifact_doc",
                    artifact_type="document",
                    title="需求文档",
                    provider="feishu",
                    status="ready",
                    url="https://feishu.example/doc",
                    version=1,
                )
            ],
            confirmations=[],
            session_documents=[],
        )
        final_detail = SimpleNamespace(task_run_id="run_delivery", session_id="s1")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.service.delivery_artifact_service.root_dir = Path(tmpdir)
            with patch.object(
                self.service.task_run_service,
                "get_task_run",
                side_effect=[detail, final_detail],
            ), patch.object(
                self.service.task_run_service,
                "upsert_step",
            ), patch.object(
                self.service.task_run_service,
                "create_artifact",
            ), patch.object(
                self.service.task_run_service,
                "update_task_run",
            ) as update_task_run:
                result = self.service.bundle_delivery_from_task_run("run_delivery", requested_by="tester")

        self.assertIs(result, final_detail)
        self.assertNotIn("stage", update_task_run.call_args.kwargs)
        self.assertNotIn("status", update_task_run.call_args.kwargs)
        self.assertIn("latest_summary", update_task_run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
