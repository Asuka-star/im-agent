from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.request_router import RouteDecision
from app.services.text_analysis import (
    build_next_actions,
    build_summary,
    extract_tasks,
    infer_risks,
    normalize_tasks,
)
from app.services.tools.task_operation_tool import TaskOperationTool

logger = logging.getLogger(__name__)


class WorkflowTaskIntentExecution:
    """Handles task update and assignment intents before the generic workflow planner runs."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def handle_task_status_update_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        workflow = self.workflow
        if not TaskOperationTool.has_status_update_signal(message.text):
            return None
        if route_decision.route not in {"tasks", "unknown"}:
            return None
        current_tasks = workflow._context_tasks_for_message(message)
        actor_names = workflow._sender_actor_names_for_message(message)
        status_update = TaskOperationTool.resolve_status_update(
            current_tasks,
            message.text,
            actor_names=actor_names,
        )
        if not status_update.get("detected"):
            return None
        clarification = status_update.get("clarification")
        if isinstance(clarification, dict):
            return workflow._pause_for_clarification(
                message,
                intent="tasks",
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=None,
                artifacts=[],
            )
        if not status_update.get("updated"):
            return None

        tasks = normalize_task_dates(normalize_tasks(status_update["tasks"]))
        updated_task = status_update.get("updated_task")
        if isinstance(updated_task, TaskItem):
            updated_task = normalize_task_dates(normalize_tasks([updated_task]))[0]
        else:
            updated_task = tasks[0] if tasks else None
        analysis = self._build_task_status_update_analysis(
            message.session_id,
            tasks,
            source_text=message.text,
            updated_task=updated_task,
        )
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            source_message_id=message.message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        reply_preview = workflow.response_formatter.format_task_status_update_reply(
            updated_task,
            str(status_update.get("status") or getattr(updated_task, "status", "done")),
        )
        return workflow.reply_sender.deliver_reply(
            message,
            "tasks",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
        )

    def handle_local_task_assignment_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        workflow = self.workflow
        if route_decision.route != "tasks":
            return None
        actor_name = workflow._primary_sender_actor_name(message)
        normalized_text = TaskOperationTool.normalize_first_person_task_text(message.text, actor_name)
        if not TaskOperationTool.has_direct_assignment_signal(normalized_text):
            return None
        incoming_tasks = normalize_tasks(extract_tasks(normalized_text))
        if not incoming_tasks:
            return None

        _, _, current_tasks = workflow._base_status_task_sources_for_message(message)
        clarification = TaskOperationTool.task_assignment_clarification(current_tasks, incoming_tasks)
        if isinstance(clarification, dict):
            return workflow._pause_for_clarification(
                message,
                intent="tasks",
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=None,
                artifacts=[],
            )

        tasks = TaskOperationTool.update_current_tasks_from_discussion(
            current_tasks,
            normalized_text,
            incoming_tasks,
            actor_names=workflow._sender_actor_names_for_message(message),
        )
        tasks = normalize_task_dates(normalize_tasks(tasks))
        analysis = self._build_local_task_assignment_analysis(
            message.session_id,
            tasks,
            source_text=normalized_text,
        )
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            source_message_id=message.message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        reply_preview = workflow.response_formatter.format_analysis_reply(analysis, "tasks")
        return workflow.reply_sender.deliver_reply(
            message,
            "tasks",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
        )

    def handle_llm_task_intent_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        workflow = self.workflow
        if not self.should_run_llm_task_intent(route_decision, message.text):
            return None
        if not workflow.llm_service.is_configured():
            return None

        try:
            intent_result = workflow.llm_service.resolve_task_intent(workspace_context, message.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM task intent parsing failed, continuing with normal flow: %s", exc)
            if task_run_id:
                workflow.task_run_service.upsert_step(
                    task_run_id,
                    step_key="task_intent_guardrail",
                    title="Resolve task semantic intent",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )
            return None

        if not isinstance(intent_result, dict):
            return None

        if task_run_id:
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="task_intent_guardrail",
                title="Resolve task semantic intent",
                step_type="intent",
                status="done",
                output_payload={
                    "intent": intent_result.get("intent"),
                    "status": intent_result.get("status"),
                    "confidence": intent_result.get("confidence"),
                    "task_hint": intent_result.get("task_hint"),
                    "reason": intent_result.get("reason"),
                },
            )

        try:
            confidence = float(intent_result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.55 or intent_result.get("intent") in {None, "unknown", "task_query"}:
            return None

        if intent_result.get("intent") == "task_status_update":
            return self._apply_llm_task_status_intent(
                message,
                intent_result,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
        if intent_result.get("intent") == "task_assignment":
            return self._apply_llm_task_assignment_intent(
                message,
                intent_result,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
        return None

    def should_run_llm_task_intent(self, route_decision: RouteDecision, instruction: str) -> bool:
        if route_decision.route not in {"tasks", "unknown"}:
            return False
        if route_decision.needs_clarification and route_decision.route == "unknown":
            return False
        text = str(instruction or "").strip()
        if not text:
            return False
        if route_decision.route == "tasks":
            return True
        if TaskOperationTool.has_status_update_signal(text) or TaskOperationTool.has_direct_assignment_signal(text):
            return True
        lowered = text.lower()
        first_person = TaskOperationTool.has_first_person_reference(text) or lowered.startswith(
            ("i ", "i'", "i've", "i am ", "i can ", "my ", "me ")
        )
        taskish = first_person or TaskOperationTool.line_has_title_hint(text)
        artifactish = any(
            marker in lowered or marker in text
            for marker in (
                "ppt",
                "slides",
                "presentation",
                "canvas",
                "diagram",
                "doc",
                "document",
                "材料",
                "图",
                "画布",
                "演示",
                "文档",
            )
        ) and any(
            marker in lowered or marker in text
            for marker in (
                "make",
                "create",
                "generate",
                "draw",
                "做成",
                "生成",
                "画",
                "整理成",
            )
        )
        return taskish and not artifactish

    def _apply_llm_task_status_intent(
        self,
        message: FeishuMessageContext,
        intent_result: dict,
        *,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        workflow = self.workflow
        _, _, current_tasks = workflow._base_status_task_sources_for_message(message)
        status_update = TaskOperationTool.resolve_status_update_from_intent(
            current_tasks,
            intent_result,
            actor_names=workflow._sender_actor_names_for_message(message),
            source_text=message.text,
        )
        if not status_update.get("detected"):
            return None
        clarification = status_update.get("clarification")
        if isinstance(clarification, dict):
            return workflow._pause_for_clarification(
                message,
                intent="tasks",
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=None,
                artifacts=[],
            )
        if not status_update.get("updated"):
            return None

        tasks = normalize_task_dates(normalize_tasks(status_update["tasks"]))
        updated_task = status_update.get("updated_task")
        if isinstance(updated_task, TaskItem):
            updated_task = normalize_task_dates(normalize_tasks([updated_task]))[0]
        else:
            updated_task = tasks[0] if tasks else None
        analysis = self._build_task_status_update_analysis(
            message.session_id,
            tasks,
            source_text=message.text,
            updated_task=updated_task,
        )
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            source_message_id=message.message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        reply_preview = workflow.response_formatter.format_task_status_update_reply(
            updated_task,
            str(status_update.get("status") or getattr(updated_task, "status", "done")),
        )
        return workflow.reply_sender.deliver_reply(
            message,
            "tasks",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
        )

    def _apply_llm_task_assignment_intent(
        self,
        message: FeishuMessageContext,
        intent_result: dict,
        *,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        workflow = self.workflow
        _, _, current_tasks = workflow._base_status_task_sources_for_message(message)
        actor_names = workflow._sender_actor_names_for_message(message)
        assignment_update = TaskOperationTool.resolve_assignment_from_intent(
            current_tasks,
            intent_result,
            actor_names=actor_names,
            source_text=message.text,
        )
        clarification = assignment_update.get("clarification")
        if isinstance(clarification, dict):
            return workflow._pause_for_clarification(
                message,
                intent="tasks",
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=None,
                artifacts=[],
            )
        if assignment_update.get("updated"):
            tasks = normalize_task_dates(normalize_tasks(assignment_update["tasks"]))
            analysis = self._build_local_task_assignment_analysis(
                message.session_id,
                tasks,
                source_text=message.text,
            )
            workflow.memory_service.save_round(
                session_id=message.session_id,
                analysis=analysis,
                episode_id=active_episode_id,
                source_message_id=message.message_id,
                async_embed=True,
                preserve_unmatched_previous=False,
            )
            reply_preview = workflow.response_formatter.format_analysis_reply(analysis, "tasks")
            return workflow.reply_sender.deliver_reply(
                message,
                "tasks",
                reply_preview,
                analysis=analysis,
                episode_id=active_episode_id,
            )

        incoming_tasks = TaskOperationTool.tasks_from_assignment_intent(
            intent_result,
            actor_names=actor_names,
            source_text=message.text,
        )
        if not incoming_tasks:
            clarification = intent_result.get("clarification") if isinstance(intent_result.get("clarification"), dict) else None
            if clarification and clarification.get("needed"):
                return workflow._pause_for_clarification(
                    message,
                    intent="tasks",
                    clarification=clarification,
                    active_episode_id=active_episode_id,
                    task_run_id=task_run_id,
                    workspace_context=None,
                    artifacts=[],
                )
            return None

        clarification = TaskOperationTool.task_assignment_clarification(current_tasks, incoming_tasks)
        if isinstance(clarification, dict):
            return workflow._pause_for_clarification(
                message,
                intent="tasks",
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=None,
                artifacts=[],
            )

        tasks = TaskOperationTool.merge_assignment_items(current_tasks, incoming_tasks)
        tasks = normalize_task_dates(normalize_tasks(tasks))
        analysis = self._build_local_task_assignment_analysis(
            message.session_id,
            tasks,
            source_text=message.text,
        )
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            source_message_id=message.message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        reply_preview = workflow.response_formatter.format_analysis_reply(analysis, "tasks")
        return workflow.reply_sender.deliver_reply(
            message,
            "tasks",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
        )

    def _build_local_task_assignment_analysis(
        self,
        session_id: str,
        tasks: list[TaskItem],
        *,
        source_text: str,
    ) -> AnalyzeResponse:
        risks = infer_risks(tasks)
        summary = build_summary(source_text, tasks)
        return AnalyzeResponse(
            session_id=session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=build_next_actions(tasks, risks),
            agent_traces=[
                AgentTrace(agent="router", summary="规则识别到直接任务认领或求助表达。"),
                AgentTrace(agent="coordinator", summary=f"本地解析并合并任务：{source_text}"),
            ],
        )

    def _build_task_status_update_analysis(
        self,
        session_id: str,
        tasks: list[TaskItem],
        *,
        source_text: str,
        updated_task: TaskItem | None,
    ) -> AnalyzeResponse:
        risks = infer_risks(tasks)
        updated_label = (
            f"{updated_task.owner} - {updated_task.title}"
            if isinstance(updated_task, TaskItem)
            else "指定任务"
        )
        summary = f"已根据用户反馈更新任务状态：{updated_label}。"
        return AnalyzeResponse(
            session_id=session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=build_next_actions(tasks, risks),
            agent_traces=[
                AgentTrace(agent="router", summary="规则识别到任务状态变更，跳过只读状态查询。"),
                AgentTrace(agent="coordinator", summary=f"从消息中应用状态更新：{source_text}"),
            ],
        )
