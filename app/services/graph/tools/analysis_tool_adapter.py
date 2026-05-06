from __future__ import annotations

import logging
from typing import Any

from app.schemas.feishu_event import FeishuMessageContext
from app.services.graph.state import PlanStep, WorkerResult, WorkflowGraphState, WorkspaceCommand

logger = logging.getLogger(__name__)


class AnalysisWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        command = state.command or WorkspaceCommand()
        route = _analysis_route(command)
        workspace_context = state.context.excerpt if state.context else ""
        message = _message_context(state)
        llm_result = self._llm_result(command, route, workspace_context, message.text)
        result = self.workflow.analysis_execution.prepare_analysis_execution(
            message,
            llm_result=llm_result,
            workspace_context=workspace_context,
            active_episode_id=state.active_episode_id,
            intent=route,
        )
        analysis = result.get("analysis")
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": route,
                "reply_preview": result.get("reply_preview"),
                "analysis": analysis.model_dump(mode="json") if hasattr(analysis, "model_dump") else analysis,
                "artifacts": result.get("artifacts", []),
                "close_title": result.get("close_title"),
            },
        )

    def _llm_result(
        self,
        command: WorkspaceCommand,
        route: str,
        workspace_context: str,
        instruction: str,
    ) -> dict[str, Any]:
        raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
        llm_result: dict[str, Any] = {}
        llm_service = getattr(self.workflow, "llm_service", None)
        if llm_service is not None and getattr(llm_service, "is_configured", lambda: False)():
            try:
                llm_result = llm_service.resolve_analysis_request(workspace_context, instruction, route)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LangGraph analysis LLM request failed, using fallback payload: %s", exc)
        if not isinstance(llm_result, dict):
            llm_result = {}
        for key in ("summary", "tasks", "task_operations", "risks", "next_actions"):
            if key in raw_payload and key not in llm_result:
                llm_result[key] = raw_payload[key]
        llm_result.update(
            {
                "operation": "analyze",
                "object": route,
                "route": route,
                "reason": llm_result.get("reason") or command.reason,
            }
        )
        return llm_result


class HelpWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        command = state.command or WorkspaceCommand()
        reply_preview = self.workflow.response_formatter.format_help_reply(command.reason or None)
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "help",
                "reply_preview": reply_preview,
                "artifacts": [],
            },
        )


class ReplyWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        command = state.command or WorkspaceCommand()
        raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
        reply_preview = _first_text(
            raw_payload.get("reply_preview"),
            raw_payload.get("reply"),
            raw_payload.get("answer"),
            raw_payload.get("message"),
        )
        if not reply_preview:
            reason = command.reason or "当前请求没有命中明确的协作动作。"
            reply_preview = self.workflow.response_formatter.format_help_reply(reason)
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "help",
                "reply_preview": reply_preview,
                "artifacts": [],
            },
        )


def _analysis_route(command: WorkspaceCommand) -> str:
    if command.route in {"summary", "tasks", "risks"}:
        return command.route
    if command.object in {"summary", "tasks", "risks"}:
        return command.object
    return "summary"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _message_context(state: WorkflowGraphState) -> FeishuMessageContext:
    return FeishuMessageContext(
        message_id=state.message.message_id,
        chat_id=state.message.chat_id,
        chat_type=state.message.chat_type,
        message_type="text",
        session_id=state.message.session_id,
        sender_id=state.message.sender_id or "",
        text=state.message.text,
        raw_text=state.message.raw_text or state.message.text,
        is_mentioned=state.message.is_mentioned,
    )
