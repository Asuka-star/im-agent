import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import settings
from app.schemas.task import TaskItem
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.request_router import RouteDecision


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

    def test_ambiguous_route_pauses_before_deep_llm_resolution(self) -> None:
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
            "resolve_workspace_request",
        ) as resolve_workspace_request, patch.object(
            self.service,
            "_deliver_reply",
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
        resolve_workspace_request.assert_not_called()
        create_confirmation.assert_called_once()
        self.assertIn("整理成飞书文档", create_confirmation.call_args.kwargs["options"])
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
                "source": "rule",
                "confidence": 0.4,
                "needs_clarification": True,
                "reason": "请求较模糊，无法确定要总结、写文档还是生成演示稿。",
                "requested_outputs": [],
            },
        )

    def test_fallback_plan_for_doc_can_chain_slides(self) -> None:
        plan = self.service._resolve_execution_plan(
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
        plan = self.service._resolve_execution_plan(
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
        plan = self.service._resolve_execution_plan(
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

        plan = self.service._resolve_execution_plan(
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
        plan = self.service._resolve_execution_plan(
            intent="summary",
            reason="误判成总结",
            llm_result={"operation": "analyze", "object": "summary"},
            instruction="把刚才的内容写成文档",
        )

        self.assertEqual(plan.primary_intent, "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc"])

    def test_operation_object_protocol_derives_route_without_legacy_intent(self) -> None:
        protocol = self.service._normalize_request_protocol(
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
        protocol = self.service._normalize_request_protocol(llm_result)

        self.assertEqual(protocol.operation, "create")
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
        reply = self.service._format_status_reply("当前有什么风险", [], {"risks": ["接口联调时间紧"]})

        self.assertIn("当前风险", reply)
        self.assertIn("接口联调时间紧", reply)

    def test_read_task_route_forces_answer_status_plan(self) -> None:
        message = type(
            "FakeMessage",
            (),
            {"session_id": "s1", "message_id": "m1", "text": "查看任务列表", "chat_id": "c1", "chat_type": "p2p"},
        )()
        with patch.object(
            self.service,
            "_prepare_status_execution",
            return_value={
                "reply_preview": "【当前协作状态】",
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            },
        ) as prepare_status, patch.object(
            self.service,
            "_deliver_reply",
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
            result = self.service._execute_llm_request(
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
            self.service,
            "_prepare_analysis_execution",
            return_value={
                "reply_preview": "[tasks]",
                "analysis": None,
                "artifacts": [],
                "close_title": "tasks",
            },
        ) as prepare_analysis, patch.object(
            self.service,
            "_prepare_status_execution",
            return_value={
                "reply_preview": "should not run",
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            },
        ) as prepare_status, patch.object(
            self.service,
            "_deliver_reply",
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
            result = self.service._execute_llm_request(
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
            self.service,
            "_prepare_doc_execution",
            return_value={
                "reply_preview": "【文档同步】\n已生成文档",
                "analysis": None,
                "artifacts": [{"artifact_type": "document", "title": "文档"}],
                "close_title": "文档",
            },
        ) as prepare_doc, patch.object(
            self.service,
            "_prepare_slides_execution",
            return_value={
                "reply_preview": "【演示稿】\n已生成演示稿大纲",
                "analysis": None,
                "artifacts": [{"artifact_type": "slides_package", "title": "演示稿"}],
                "close_title": "slides",
            },
        ) as prepare_slides, patch.object(
            self.service,
            "_deliver_reply",
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
            result = self.service._execute_llm_request(
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
            self.service,
            "_deliver_reply",
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
            self.service,
            "_prepare_doc_execution",
            return_value={
                "reply_preview": "【文档同步】\n已修订",
                "analysis": None,
                "artifacts": [{"artifact_type": "document", "title": "协作文档"}],
            },
        ) as prepare_doc, patch.object(
            self.service,
            "_persist_task_run_result",
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
            },
        )


if __name__ == "__main__":
    unittest.main()
