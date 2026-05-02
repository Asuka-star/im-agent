from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task_run import ArtifactRecord
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkflowReplySender:
    """Handles Feishu reply delivery and preview-only next-action augmentation."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def deliver_reply(
        self,
        message: FeishuMessageContext,
        mode: str,
        reply_preview: str | None,
        *,
        analysis: AnalyzeResponse | None,
        episode_id: int | None = None,
        artifacts: list[dict] | None = None,
        append_next_actions: bool = True,
    ) -> dict:
        workflow = self.workflow
        reply_sent = False
        reply_error: str | None = None
        if append_next_actions:
            reply_preview = self.append_next_actions_to_reply(
                message,
                mode=mode,
                reply_preview=reply_preview,
                artifacts=artifacts,
            )

        if reply_preview and settings.feishu_reply_enabled and message.chat_id:
            try:
                workflow.message_api.send_text_message(
                    message.chat_id,
                    reply_preview,
                    receive_id_type="chat_id",
                )
                reply_sent = True
            except Exception as exc:  # noqa: BLE001
                reply_error = str(exc)
                logger.exception("Failed to send Feishu reply")

        return {
            "session_id": message.session_id,
            "episode_id": episode_id,
            "mode": mode,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
            "artifacts": artifacts or [],
        }

    def append_next_actions_to_reply(
        self,
        message: FeishuMessageContext,
        *,
        mode: str,
        reply_preview: str | None,
        artifacts: list[dict] | None,
    ) -> str | None:
        workflow = self.workflow
        if not reply_preview or "我建议下一步可以：" in reply_preview:
            return reply_preview
        if mode in {"status", "help", "speech_notice"}:
            return reply_preview
        detail = workflow.status_execution.synthetic_task_run_detail_for_message(message).model_copy(
            update={
                "intent": mode,
                "title": workflow._task_run_title(message.text, mode),
                "latest_reply_preview": reply_preview,
                "artifacts": self.artifact_records_from_payloads(artifacts or []),
            }
        )
        bundle = workflow.next_action_service.build_for_task_run(detail)
        return workflow.response_formatter.append_next_actions(reply_preview, bundle)

    @staticmethod
    def artifact_records_from_payloads(artifacts: list[dict]) -> list[ArtifactRecord]:
        records: list[ArtifactRecord] = []
        for index, artifact in enumerate(artifacts, start=1):
            if not isinstance(artifact, dict):
                continue
            artifact_type = str(artifact.get("artifact_type") or "note").strip() or "note"
            records.append(
                ArtifactRecord(
                    artifact_id=str(artifact.get("artifact_id") or f"pending_{index}_{artifact_type}"),
                    artifact_type=artifact_type,
                    provider=str(artifact.get("provider") or "local"),
                    title=str(artifact.get("title") or "协作产物"),
                    status=str(artifact.get("status") or "ready"),
                    url=str(artifact.get("url") or "").strip() or None,
                    version=coerce_positive_int(artifact.get("version")),
                )
            )
        return records
