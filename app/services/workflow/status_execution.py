from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.schemas.task_run import TaskRunDetail
from app.services.text_analysis import build_next_actions, infer_risks

logger = logging.getLogger(__name__)


class WorkflowStatusExecution:
    """Builds status answers and next-action snapshots for workflow steps."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_status_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        active_episode_id: int | None = None,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        next_action_reply = self.next_action_reply_for_status_query(message, task_run_id=task_run_id)
        if next_action_reply:
            return {
                "reply_preview": next_action_reply,
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            }
        clarification = workflow._pending_task_status_update_clarification_for_message(message)
        if clarification:
            return {
                "reply_preview": workflow.response_formatter.format_clarification_reply(
                    intent="tasks",
                    clarification=clarification,
                ),
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            }
        tasks = workflow._context_tasks_for_message(message)
        payload = workflow._context_payload_for_message(message)
        status_answer = workflow.response_formatter.format_status_reply(message.text, tasks, payload)
        if active_episode_id is not None and tasks:
            self.persist_status_task_snapshot(
                message.session_id,
                tasks,
                episode_id=active_episode_id,
            )
        return {
            "reply_preview": status_answer,
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }
    def next_action_reply_for_status_query(self, message: FeishuMessageContext, *, task_run_id: str | None) -> str | None:
        workflow = self.workflow
        if not self.is_next_action_query(message.text):
            return None
        detail = workflow.task_run_service.get_task_run(task_run_id) if task_run_id else None
        if detail is None:
            detail = self.synthetic_task_run_detail_for_message(message)
        bundle = workflow.next_action_service.build_for_task_run(detail)
        return workflow.response_formatter.format_next_action_block(bundle) or None
    @staticmethod
    def is_next_action_query(text: str) -> bool:
        query = str(text or "").strip().lower()
        if not query:
            return False
        return any(
            marker in query
            for marker in (
                "下一步",
                "下一步行动",
                "下一步建议",
                "接下来",
                "推荐",
                "next action",
                "next step",
            )
        )
    def synthetic_task_run_detail_for_message(self, message: FeishuMessageContext) -> TaskRunDetail:
        workflow = self.workflow
        try:
            payloads = workflow.session_document_service.list_documents(message.session_id)
            documents = [workflow.task_run_service._session_document_from_payload(item) for item in payloads]
        except Exception:
            documents = []
        return TaskRunDetail(
            task_run_id=f"synthetic_{message.message_id or message.session_id}",
            session_id=message.session_id,
            source_type=getattr(message, "chat_type", None) or "unknown",
            source_ref=getattr(message, "chat_id", None),
            trigger_message_id=getattr(message, "message_id", None),
            intent="status",
            title="上下文下一步行动推荐",
            stage="recommendation",
            status="completed",
            latest_summary="",
            latest_reply_preview="",
            latest_error=None,
            session_documents=documents,
        )
    def persist_status_task_snapshot(
        self,
        session_id: str,
        tasks: list,
        *,
        episode_id: int | None,
    ) -> None:
        workflow = self.workflow
        normalized_tasks: list[TaskItem] = []
        for task in tasks:
            if isinstance(task, TaskItem):
                normalized_tasks.append(task)
                continue
            try:
                normalized_tasks.append(TaskItem.model_validate(task))
            except Exception:
                continue
        if not normalized_tasks:
            return
        risks = infer_risks(normalized_tasks)
        analysis = AnalyzeResponse(
            session_id=session_id,
            summary="根据当前文档和最新讨论刷新任务快照。",
            tasks=normalized_tasks,
            risks=risks,
            next_actions=build_next_actions(normalized_tasks, risks),
            agent_traces=[AgentTrace(agent="status", summary="persisted task snapshot from status query")],
        )
        try:
            workflow.memory_service.save_round(
                session_id=session_id,
                analysis=analysis,
                episode_id=episode_id,
                embed=False,
                async_embed=False,
                preserve_unmatched_previous=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist status task snapshot: session_id=%s error=%s", session_id, exc)
