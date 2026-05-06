from __future__ import annotations

import json
import logging
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
        task_run_id: str | None = None,
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
                task_run_id=task_run_id,
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
                        task_run_id=task_run_id,
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
        task_run_id: str | None = None,
    ) -> bool:
        if not message.chat_id:
            return False
        checks: list[dict] = []
        if hasattr(self.workflow, "status_execution"):
            title = (
                self.workflow._task_run_title(message.text, mode)
                if hasattr(self.workflow, "_task_run_title")
                else mode
            )
            detail = self._synthetic_detail_for_reply(
                message,
                mode=mode,
                title=title,
                reply_preview=reply_preview,
                artifacts=artifacts or [],
                task_run_id=task_run_id,
            )
            checks = [
                item.model_dump() if hasattr(item, "model_dump") else item
                for item in self.artifact_verifier.build_for_task_run(detail)
            ]
        card = self.card_builder.build_artifact_card(
            title="AI 协作产物已生成",
            mode=mode,
            artifacts=artifacts,
            summary=reply_preview,
            task_run_id=task_run_id,
            source_message_id=getattr(message, "message_id", None),
            session_id=message.session_id,
            checks=checks,
        )
        if card is None:
            return False
        self.workflow.message_api.send_interactive_message(
            message.chat_id,
            card,
            receive_id_type="chat_id",
        )
        return True

    def send_clarification_card(
        self,
        message: FeishuMessageContext,
        *,
        intent: str,
        clarification: dict,
        task_run_id: str | None = None,
        confirmation_id: str | None = None,
    ) -> bool:
        if not message.chat_id:
            return False
        candidates = clarification.get("candidates") if isinstance(clarification.get("candidates"), list) else []
        raw_target_status = clarification.get("target_status") or clarification.get("status")
        target_status = str(raw_target_status or "").strip().lower()
        status_confirmation = target_status.strip().lower() in {"done", "cancelled", "canceled"}
        if intent == "tasks" and candidates and status_confirmation:
            card = self.card_builder.build_task_confirmation_card(
                session_id=message.session_id,
                task_run_id=task_run_id,
                source_message_id=message.message_id,
                question=str(clarification.get("question") or "请确认任务更新"),
                reason=str(clarification.get("reason") or ""),
                candidates=candidates,
                target_status=target_status,
                confirmation_id=confirmation_id,
            )
        else:
            options = clarification.get("options") if isinstance(clarification.get("options"), list) else []
            card = self.card_builder.build_clarification_card(
                session_id=message.session_id,
                task_run_id=task_run_id,
                source_message_id=message.message_id,
                question=str(clarification.get("question") or "请确认下一步"),
                reason=str(clarification.get("reason") or ""),
                options=[str(item) for item in options],
                confirmation_id=confirmation_id,
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
        task_run_id: str | None = None,
    ) -> str | None:
        workflow = self.workflow
        if not reply_preview or "我建议下一步可以：" in reply_preview:
            return reply_preview
        if mode in {"status", "help", "speech_notice"}:
            return reply_preview
        detail = self._synthetic_detail_for_reply(
            message,
            mode=mode,
            title=workflow._task_run_title(message.text, mode),
            reply_preview=reply_preview,
            artifacts=artifacts or [],
            task_run_id=task_run_id,
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

    def _synthetic_detail_for_reply(
        self,
        message: FeishuMessageContext,
        *,
        mode: str,
        title: str,
        reply_preview: str | None,
        artifacts: list[dict],
        task_run_id: str | None,
    ) -> Any:
        detail = self.workflow.status_execution.synthetic_task_run_detail_for_message(message)
        return detail.model_copy(
            update={
                "task_run_id": task_run_id or f"synthetic_{getattr(message, 'message_id', None) or message.session_id}",
                "intent": mode,
                "title": title,
                "latest_reply_preview": reply_preview,
                "artifacts": self.artifact_records_from_payloads(artifacts),
                "session_documents": self._reply_session_documents(
                    message,
                    artifacts=artifacts,
                    task_run_id=task_run_id,
                ),
            }
        )

    def _reply_session_documents(
        self,
        message: FeishuMessageContext,
        *,
        artifacts: list[dict],
        task_run_id: str | None,
    ) -> list[Any]:
        workflow = self.workflow
        try:
            payloads = workflow.session_document_service.list_documents(message.session_id)
            documents = [workflow.task_run_service._session_document_from_payload(item) for item in payloads]
        except Exception:
            return []

        candidate_signatures = self._document_candidate_signatures(artifacts)
        requirement_loader = getattr(workflow, "_requirement_document_target_for_task_run", None)
        if task_run_id and callable(requirement_loader):
            try:
                target = requirement_loader(task_run_id)
            except Exception:  # noqa: BLE001
                target = None
            signature = self._document_signature(target)
            if signature is not None:
                candidate_signatures.add(signature)

        if not candidate_signatures:
            return documents
        filtered = [
            document
            for document in documents
            if (signature := self._document_signature(document)) is not None and signature in candidate_signatures
        ]
        return filtered or documents

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

    @staticmethod
    def _document_candidate_signatures(artifacts: list[dict]) -> set[tuple[str, str]]:
        signatures: set[tuple[str, str]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = str(artifact.get("artifact_type") or "").strip()
            if artifact_type not in {"document", "doc", "feishu_doc"}:
                continue
            signature = WorkflowReplySender._document_signature(artifact)
            if signature is not None:
                signatures.add(signature)
        return signatures

    @staticmethod
    def _document_signature(source: Any) -> tuple[str, str] | None:
        if isinstance(source, dict):
            preview = source.get("preview") if isinstance(source.get("preview"), dict) else {}
            sync = preview.get("sync") if isinstance(preview.get("sync"), dict) else {}
            document_id = str(source.get("document_id") or sync.get("document_id") or "").strip()
            url = str(source.get("url") or sync.get("url") or preview.get("url") or "").strip()
            title = str(source.get("title") or sync.get("title") or preview.get("title") or "").strip().lower()
        else:
            document_id = str(getattr(source, "document_id", None) or "").strip()
            url = str(getattr(source, "url", None) or "").strip()
            title = str(getattr(source, "title", None) or "").strip().lower()
        if document_id:
            return ("document_id", document_id)
        if url:
            return ("url", url)
        if title:
            return ("title", title)
        return None

    def _check_status_label(self, status: str) -> str:
        if status == "ready":
            return "已满足"
        if status == "partial":
            return "部分满足"
        if status == "missing":
            return "待补齐"
        return status or "未知"
