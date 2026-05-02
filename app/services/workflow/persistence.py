from __future__ import annotations

import logging
from typing import Any

from app.services.next_action_service import ContextualNextActionService
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkflowResultPersistence:
    """Persists workflow replies, artifacts, and follow-up recommendations."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def persist_task_run_result(self, task_run_id: str, *, message_text: str, result: dict, session_id: str) -> None:
        workflow = self.workflow
        if result["reply_preview"]:
            workflow.memory_service.save_assistant_message(
                session_id=session_id,
                content=result["reply_preview"],
                episode_id=result.get("episode_id"),
                embed=False,
            )
        self.persist_artifacts(task_run_id, result.get("artifacts", []))
        summary_text = (
            result["analysis"].summary
            if result.get("analysis") is not None
            else workflow._condense_text(result.get("reply_preview"))
        )
        response_step_status = str(result.get("response_step_status") or "done")
        final_stage = str(result.get("task_run_stage") or "delivered")
        final_status = str(result.get("task_run_status") or "completed")
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="response_generated",
            title="生成处理结果",
            step_type="workflow",
            status=response_step_status,
            output_payload={
                "mode": result["mode"],
                "reply_preview": result["reply_preview"],
                "artifact_count": len(result.get("artifacts", [])),
            },
        )
        if result.get("artifacts"):
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="artifact_persisted",
                title="记录协作产物",
                step_type="artifact",
                status="done",
                output_payload={"artifact_count": len(result["artifacts"])},
            )
        workflow.task_run_service.update_task_run(
            task_run_id,
            intent=result["mode"],
            title=workflow._task_run_title(message_text, result["mode"]),
            stage=final_stage,
            status=final_status,
            latest_summary=summary_text,
            latest_reply_preview=result.get("reply_preview"),
            latest_error=result.get("reply_error"),
        )
        self.store_next_action_recommendations(task_run_id)

    def store_next_action_recommendations(self, task_run_id: str) -> None:
        workflow = self.workflow
        try:
            detail = workflow.task_run_service.get_task_run(task_run_id)
            if detail is None:
                return
            required_fields = ("artifacts", "confirmations", "session_documents", "steps")
            if any(not hasattr(detail, field) for field in required_fields):
                return
            bundle = workflow.next_action_service.build_for_task_run(detail)
            workflow.task_run_service.merge_task_run_metadata(
                task_run_id,
                {"recommendations": ContextualNextActionService.bundle_to_metadata(bundle)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipped next-action recommendation snapshot: task_run_id=%s error=%s", task_run_id, exc)

    def persist_artifacts(self, task_run_id: str, artifacts: list | None) -> None:
        workflow = self.workflow
        for artifact in artifacts or []:
            if not isinstance(artifact, dict):
                continue
            workflow.task_run_service.create_artifact(
                task_run_id,
                artifact_type=str(artifact.get("artifact_type") or "note"),
                title=str(artifact.get("title") or "协作产物"),
                provider=str(artifact.get("provider") or "local"),
                status=str(artifact.get("status") or "ready"),
                url=str(artifact.get("url") or "").strip() or None,
                preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
                version=coerce_positive_int(artifact.get("version")),
            )
