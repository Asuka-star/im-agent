from __future__ import annotations

import logging
import json
from typing import Any

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task_run import ArtifactRecord
from app.services.feishu_card_builder import FeishuArtifactCardBuilder
from app.services.task_artifact_verifier import TaskArtifactVerifier
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkflowReplySender:
    """Handles Feishu reply delivery and preview-only next-action augmentation."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow
        self.artifact_verifier = TaskArtifactVerifier()
        self.card_builder = FeishuArtifactCardBuilder()

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
        reply_card_sent = False
        reply_card_error: str | None = None
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
            if settings.feishu_reply_card_enabled and artifacts:
                try:
                    reply_card_sent = self.send_artifact_card(
                        message,
                        mode=mode,
                        reply_preview=reply_preview,
                        artifacts=artifacts,
                    )
                except Exception as exc:  # noqa: BLE001
                    reply_card_error = str(exc)
                    logger.exception("Failed to send Feishu artifact card")

        return {
            "session_id": message.session_id,
            "episode_id": episode_id,
            "mode": mode,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
            "reply_card_sent": reply_card_sent,
            "reply_card_error": reply_card_error,
            "artifacts": artifacts or [],
        }

    def send_artifact_card(
        self,
        message: FeishuMessageContext,
        *,
        mode: str,
        reply_preview: str | None,
        artifacts: list[dict],
    ) -> bool:
        if not message.chat_id:
            return False
        card = self.card_builder.build_artifact_card(
            title="AI 协作产物已生成",
            mode=mode,
            artifacts=artifacts,
            summary=reply_preview,
        )
        if card is None:
            return False
        self.workflow.message_api.send_interactive_message(
            message.chat_id,
            card,
            receive_id_type="chat_id",
        )
        return True

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
        reply_preview = self.append_artifact_checks_to_reply(reply_preview, detail)
        bundle = workflow.next_action_service.build_for_task_run(detail)
        return workflow.response_formatter.append_next_actions(reply_preview, bundle)

    def append_artifact_checks_to_reply(self, reply_preview: str, detail: Any) -> str:
        if "验收摘要：" in reply_preview:
            return reply_preview
        artifacts = list(getattr(detail, "artifacts", []) or [])
        if not artifacts:
            return reply_preview
        checks = self.artifact_verifier.build_for_task_run(detail)
        visible_checks = [
            item
            for item in checks
            if item.get("key") in {"document", "canvas", "slides", "rehearsal", "delivery_bundle"}
        ]
        if not visible_checks:
            return reply_preview
        ready_count = sum(1 for item in visible_checks if item.get("status") == "ready")
        lines = [
            "",
            "验收摘要：",
            f"- 产物检查：{ready_count}/{len(visible_checks)} 项已满足",
        ]
        for item in visible_checks:
            label = str(item.get("label") or item.get("key") or "检查项")
            status = self._check_status_label(str(item.get("status") or "missing"))
            detail_text = str(item.get("detail") or "").strip()
            suffix = f"（{detail_text}）" if detail_text else ""
            lines.append(f"- {label}：{status}{suffix}")
        return reply_preview.rstrip() + "\n" + "\n".join(lines)

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
                    preview_json=(
                        json.dumps(artifact.get("preview"), ensure_ascii=False)
                        if isinstance(artifact.get("preview"), dict)
                        else None
                    ),
                )
            )
        return records

    def _check_status_label(self, status: str) -> str:
        if status == "ready":
            return "已满足"
        if status == "partial":
            return "部分满足"
        if status == "missing":
            return "待补齐"
        return status or "未知"
