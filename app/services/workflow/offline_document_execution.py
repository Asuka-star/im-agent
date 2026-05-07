from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.schemas.feishu_event import FeishuMessageContext

logger = logging.getLogger(__name__)


class WorkflowOfflineDocumentExecution:
    """Handles offline document uploads before they enter the full merge workflow."""

    APPLY_OPTION = "仅更新当前文档"
    APPLY_WITH_SLIDES_OPTION = "更新文档 + 当前 PPT"
    APPLY_WITH_CANVAS_OPTION = "更新文档 + 当前 Canvas"
    APPLY_WITH_SLIDES_AND_CANVAS_OPTION = "更新文档 + 当前 PPT + 当前 Canvas"
    ARCHIVE_OPTION = "仅作为参考材料暂存"
    CONFIRMATION_PROMPT = "已收到离线文档，请选择处理方式：仅更新当前文档、联动当前 PPT / Canvas，或先暂存为参考材料。"

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_offline_document_submission(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str,
    ) -> dict:
        workflow = self.workflow
        file_key = str(message.file_key or "").strip()
        file_name = str(message.file_name or "").strip()
        if not file_key or not file_name:
            return self._fail_submission(
                message,
                task_run_id=task_run_id,
                detail="离线文档消息缺少 file_key 或 file_name。",
            )

        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_received",
            title="接收离线文档",
            step_type="offline_document",
            status="done",
            input_payload={
                "message_id": message.message_id,
                "file_key": file_key,
                "file_name": file_name,
            },
        )
        workflow.task_run_service.update_task_run(task_run_id, stage="offline_document_downloading", status="running")

        try:
            file_bytes, content_type = workflow.message_resource_api.download_message_resource(
                message_id=str(message.message_id or ""),
                file_key=file_key,
                resource_type="file",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to download offline document: message_id=%s error=%s", message.message_id, exc)
            return self._fail_submission(
                message,
                task_run_id=task_run_id,
                detail=f"离线文档下载失败：{exc}",
            )

        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_downloaded",
            title="下载离线文档",
            step_type="offline_document",
            status="done",
            output_payload={"content_type": content_type, "byte_count": len(file_bytes)},
        )
        workflow.task_run_service.update_task_run(task_run_id, stage="offline_document_parsing", status="running")
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        requirement = self._load_requirement_for_task_run(task_run_id)
        duplicate = self._find_duplicate_submission(
            task_run_id,
            requirement=requirement,
            session_id=message.session_id,
            file_sha256=file_sha256,
        )
        if duplicate is not None:
            return self._complete_duplicate_submission(
                message,
                task_run_id=task_run_id,
                file_name=file_name,
                content_type=content_type,
                file_sha256=file_sha256,
                duplicate=duplicate,
            )

        try:
            parsed = workflow.offline_document_parser.parse_bytes(
                file_name=file_name,
                content=file_bytes,
                content_type=content_type,
            )
        except ValueError as exc:
            return self._fail_submission(
                message,
                task_run_id=task_run_id,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse offline document: file_name=%s error=%s", file_name, exc)
            return self._fail_submission(
                message,
                task_run_id=task_run_id,
                detail=f"离线文档解析失败：{exc}",
            )

        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_parsed",
            title="解析离线文档",
            step_type="offline_document",
            status="done",
            output_payload={
                "file_name": parsed.file_name,
                "file_extension": parsed.file_extension,
                "section_count": parsed.section_count,
                "paragraph_count": parsed.paragraph_count,
                "headings": parsed.headings[:8],
            },
        )

        current_document = self._resolve_current_document(task_run_id, requirement=requirement)
        merge_plan = workflow.offline_document_merge_service.build_merge_plan(
            parsed.package,
            current_document=current_document,
            instruction=message.text,
        )
        merge_summary = merge_plan
        options = self._build_confirmation_options(requirement)
        available_follow_up_targets = self._available_follow_up_targets(requirement)
        confirmation = workflow.task_run_service.create_confirmation(
            task_run_id,
            prompt=self.CONFIRMATION_PROMPT,
            options=options,
        )
        metadata = workflow.task_run_service.get_task_run_metadata(task_run_id)
        metadata["offline_document_confirmation"] = {
            "confirmation_id": confirmation.confirmation_id,
            "instruction": message.text,
            "file_name": parsed.file_name,
            "file_extension": parsed.file_extension,
            "content_type": parsed.content_type,
            "file_sha256": file_sha256,
            "package": parsed.package,
            "options": options,
            "available_follow_up_targets": sorted(available_follow_up_targets),
            "merge_summary": merge_summary,
            "merge_plan": merge_plan,
        }
        workflow.task_run_service.update_task_run(
            task_run_id,
            metadata=metadata,
            stage="offline_document_confirmation",
            status="waiting_confirmation",
            latest_summary=self._offline_confirmation_latest_summary(parsed.file_name, merge_summary),
        )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_confirmation",
            title="确认离线文档处理方式",
            step_type="confirmation",
            status="pending",
            output_payload={
                "question": self.CONFIRMATION_PROMPT,
                "options": options,
                "file_name": parsed.file_name,
                "available_follow_up_targets": sorted(available_follow_up_targets),
                "merge_summary": merge_summary,
                "merge_plan": merge_plan,
            },
        )

        clarification = {
            "question": self.CONFIRMATION_PROMPT,
            "reason": self._parsed_document_summary(
                parsed,
                available_follow_up_targets=available_follow_up_targets,
                merge_summary=merge_summary,
            ),
            "options": options,
        }
        reply_preview = workflow.response_formatter.format_clarification_reply(
            intent="doc",
            clarification=clarification,
        )
        result = workflow.reply_sender.deliver_reply(
            message,
            "offline_document",
            reply_preview,
            analysis=None,
            artifacts=[],
            append_next_actions=False,
            task_run_id=task_run_id,
        )
        if settings.feishu_reply_enabled and settings.feishu_reply_card_enabled:
            try:
                result["reply_card_sent"] = workflow.reply_sender.send_clarification_card(
                    message,
                    intent="doc",
                    clarification=clarification,
                    task_run_id=task_run_id,
                    confirmation_id=confirmation.confirmation_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to send offline document clarification card")
                result["reply_card_error"] = str(exc)
        result["pending_confirmation"] = True
        result["confirmation_id"] = confirmation.confirmation_id
        result["response_step_status"] = "pending"
        result["task_run_status"] = "waiting_confirmation"
        result["task_run_stage"] = "offline_document_confirmation"
        result["task_run_id"] = task_run_id
        return result

    def _complete_duplicate_submission(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str,
        file_name: str,
        content_type: str,
        file_sha256: str,
        duplicate: dict[str, Any],
    ) -> dict:
        workflow = self.workflow
        duplicate_submission_id = str(duplicate.get("submission_id") or "").strip()
        duplicate_status = str(duplicate.get("status") or "").strip() or "processed"
        summary_text = self._duplicate_submission_latest_summary(
            file_name,
            duplicate_submission_id=duplicate_submission_id,
            duplicate_status=duplicate_status,
        )
        metadata = workflow.task_run_service.get_task_run_metadata(task_run_id)
        metadata.pop("offline_document_confirmation", None)
        metadata["offline_document_last_record"] = {
            "submission_id": task_run_id,
            "task_run_id": task_run_id,
            "file_name": file_name,
            "file_extension": Path(file_name).suffix.lower() or None,
            "content_type": content_type,
            "file_sha256": file_sha256,
            "status": "ignored_duplicate",
            "duplicate_of_submission_id": duplicate_submission_id or None,
            "duplicate_of_status": duplicate_status,
            "merge_summary": duplicate.get("merge_summary") if isinstance(duplicate.get("merge_summary"), dict) else {},
            "merge_plan": duplicate.get("merge_plan") if isinstance(duplicate.get("merge_plan"), dict) else {},
        }
        workflow.task_run_service.update_task_run(
            task_run_id,
            metadata=metadata,
            stage="offline_document_duplicate",
            status="completed",
            latest_summary=summary_text,
        )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_duplicate",
            title="识别重复上传",
            step_type="offline_document",
            status="done",
            output_payload={
                "file_name": file_name,
                "duplicate_of_submission_id": duplicate_submission_id or None,
                "duplicate_of_status": duplicate_status,
            },
        )
        result = workflow.reply_sender.deliver_reply(
            message,
            "offline_document",
            summary_text,
            analysis=None,
            artifacts=[],
            append_next_actions=False,
            task_run_id=task_run_id,
        )
        result["task_run_status"] = "completed"
        result["task_run_stage"] = "offline_document_duplicate"
        result["response_step_status"] = "done"
        result["task_run_id"] = task_run_id
        return result

    def parsed_package_local_artifact(
        self,
        *,
        task_run_id: str,
        session_id: str,
        package: dict,
        file_name: str,
        summary_lines: list[str],
    ) -> dict:
        local_artifact = self.workflow.office_artifact_service.persist_document_markdown(
            package,
            sync_lines=summary_lines,
            task_run_id=task_run_id,
            session_id=session_id,
        )
        preview = dict(package)
        preview["sync"] = {
            "mode": "offline_reference",
            "status": "ready",
            "file_name": file_name,
            "sync_lines": summary_lines,
        }
        return {
            "artifact_type": "document",
            "provider": "offline_upload",
            "status": "ready",
            "title": str(package.get("title") or file_name or "离线文档").strip() or "离线文档",
            "url": local_artifact.get("url"),
            "preview": preview,
            "version": 1,
        }

    def _fail_submission(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str,
        detail: str,
    ) -> dict:
        workflow = self.workflow
        workflow.task_run_service.update_task_run(
            task_run_id,
            stage="offline_document_failed",
            status="failed",
            latest_error=detail,
        )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="offline_document_failed",
            title="离线文档处理失败",
            step_type="offline_document",
            status="failed",
            error=detail,
        )
        result = workflow.reply_sender.deliver_reply(
            message,
            "offline_document",
            detail,
            analysis=None,
            artifacts=[],
            append_next_actions=False,
            task_run_id=task_run_id,
        )
        result["task_run_status"] = "failed"
        result["task_run_stage"] = "offline_document_failed"
        result["task_run_id"] = task_run_id
        return result

    @staticmethod
    def _parsed_document_summary(
        parsed: Any,
        *,
        available_follow_up_targets: set[str] | None = None,
        merge_summary: dict[str, Any] | None = None,
    ) -> str:
        headings = "、".join(item for item in parsed.headings[:4] if str(item).strip())
        summary = [
            f"文件：{parsed.file_name}",
            f"格式：{parsed.file_extension.lstrip('.') or 'unknown'}",
            f"章节数：{parsed.section_count}",
            f"段落数：{parsed.paragraph_count}",
        ]
        if headings:
            summary.append(f"识别到的主要章节：{headings}")
        targets = available_follow_up_targets or set()
        if targets == {"slides", "canvas"}:
            summary.append("当前需求下已有 PPT 和 Canvas，可按确认结果联动更新")
        elif targets == {"slides"}:
            summary.append("当前需求下已有 PPT，可按确认结果联动更新")
        elif targets == {"canvas"}:
            summary.append("当前需求下已有 Canvas，可按确认结果联动更新")
        if isinstance(merge_summary, dict):
            for line in merge_summary.get("summary_lines", [])[:3]:
                text = str(line).strip()
                if text:
                    summary.append(text)
        return "；".join(summary)

    def _resolve_current_document(self, task_run_id: str, *, requirement: Any | None) -> dict[str, Any] | None:
        current_document = getattr(requirement, "current_document", None) if requirement is not None else None
        payload = self._document_payload(current_document)
        if payload and payload.get("document_id"):
            return payload
        try:
            detail = self.workflow.task_run_service.get_task_run(task_run_id)
        except Exception:  # noqa: BLE001
            return None
        documents = getattr(detail, "session_documents", []) or []
        for item in documents:
            payload = self._document_payload(item)
            if payload and payload.get("is_current") and payload.get("document_id"):
                return payload
        for item in documents:
            payload = self._document_payload(item)
            if payload and payload.get("document_id"):
                return payload
        return None

    @staticmethod
    def _document_payload(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            payload = dump(mode="json")
            return payload if isinstance(payload, dict) else None
        return None

    @staticmethod
    def _offline_confirmation_latest_summary(file_name: str, merge_summary: dict[str, Any]) -> str:
        summary_lines = merge_summary.get("summary_lines") if isinstance(merge_summary, dict) else []
        if isinstance(summary_lines, list) and summary_lines:
            return f"已解析离线文档《{file_name}》：{str(summary_lines[0]).strip()}。等待确认后继续处理。"
        return f"已解析离线文档《{file_name}》，等待确认后继续处理。"

    @staticmethod
    def _duplicate_submission_latest_summary(file_name: str, *, duplicate_submission_id: str, duplicate_status: str) -> str:
        duplicate_hint = f"（重复于 {duplicate_submission_id}）" if duplicate_submission_id else ""
        return f"已识别离线文档《{file_name}》与已有回传内容一致{duplicate_hint}，本次上传已忽略。原记录状态：{duplicate_status}。"

    def _load_requirement_for_task_run(self, task_run_id: str) -> Any | None:
        workflow = self.workflow
        requirement_service = getattr(workflow, "requirement_service", None)
        if requirement_service is None:
            return None
        try:
            detail = workflow.task_run_service.get_task_run(task_run_id)
        except Exception:  # noqa: BLE001
            return None
        requirement_id = str(getattr(detail, "requirement_id", "") or "").strip()
        if not requirement_id:
            return None
        try:
            return requirement_service.get_requirement(requirement_id)
        except Exception:  # noqa: BLE001
            return None

    def _build_confirmation_options(self, requirement: Any | None) -> list[str]:
        options = [self.APPLY_OPTION]
        targets = self._available_follow_up_targets(requirement)
        if "slides" in targets:
            options.append(self.APPLY_WITH_SLIDES_OPTION)
        if "canvas" in targets:
            options.append(self.APPLY_WITH_CANVAS_OPTION)
        if targets == {"slides", "canvas"}:
            options.append(self.APPLY_WITH_SLIDES_AND_CANVAS_OPTION)
        options.append(self.ARCHIVE_OPTION)
        return options

    @staticmethod
    def _available_follow_up_targets(requirement: Any | None) -> set[str]:
        targets: set[str] = set()
        if requirement is None:
            return targets
        if str(getattr(getattr(requirement, "current_slides", None), "artifact_id", "") or "").strip():
            targets.add("slides")
        if str(getattr(getattr(requirement, "current_canvas", None), "artifact_id", "") or "").strip():
            targets.add("canvas")
        return targets

    def _find_duplicate_submission(
        self,
        task_run_id: str,
        *,
        requirement: Any | None,
        session_id: str,
        file_sha256: str,
    ) -> dict[str, Any] | None:
        if not file_sha256:
            return None
        task_run_service = getattr(self.workflow, "task_run_service", None)
        list_task_runs = getattr(task_run_service, "list_task_runs", None)
        get_task_run = getattr(task_run_service, "get_task_run", None)
        if not callable(list_task_runs) or not callable(get_task_run):
            return None
        requirement_id = str(getattr(requirement, "requirement_id", "") or "").strip()
        try:
            candidates = (
                list_task_runs(requirement_id=requirement_id, limit=50)
                if requirement_id
                else list_task_runs(session_id=session_id, limit=50)
            )
        except Exception:  # noqa: BLE001
            return None
        for candidate in candidates or []:
            candidate_id = str(getattr(candidate, "task_run_id", "") or "").strip()
            if not candidate_id or candidate_id == task_run_id:
                continue
            try:
                detail = get_task_run(candidate_id)
            except Exception:  # noqa: BLE001
                continue
            if detail is None:
                continue
            active_payload, last_payload = self._offline_submission_payloads(detail)
            hashes = {
                str(active_payload.get("file_sha256") or "").strip(),
                str(last_payload.get("file_sha256") or "").strip(),
            }
            if file_sha256 not in {value for value in hashes if value}:
                continue
            merge_summary = active_payload.get("merge_summary") if isinstance(active_payload.get("merge_summary"), dict) else {}
            if not merge_summary and isinstance(last_payload.get("merge_summary"), dict):
                merge_summary = last_payload["merge_summary"]
            merge_plan = active_payload.get("merge_plan") if isinstance(active_payload.get("merge_plan"), dict) else {}
            if not merge_plan and isinstance(last_payload.get("merge_plan"), dict):
                merge_plan = last_payload["merge_plan"]
            return {
                "submission_id": candidate_id,
                "status": self._offline_submission_status(detail, active_payload=active_payload, last_payload=last_payload),
                "merge_summary": merge_summary,
                "merge_plan": merge_plan,
            }
        return None

    @staticmethod
    def _offline_submission_payloads(detail: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata_json = str(getattr(detail, "metadata_json", "") or "").strip()
        if not metadata_json:
            return {}, {}
        try:
            import json

            metadata = json.loads(metadata_json)
        except Exception:  # noqa: BLE001
            return {}, {}
        if not isinstance(metadata, dict):
            return {}, {}
        active_payload = metadata.get("offline_document_confirmation")
        last_payload = metadata.get("offline_document_last_record")
        return (
            active_payload if isinstance(active_payload, dict) else {},
            last_payload if isinstance(last_payload, dict) else {},
        )

    @staticmethod
    def _offline_submission_status(
        detail: Any,
        *,
        active_payload: dict[str, Any],
        last_payload: dict[str, Any],
    ) -> str:
        detail_status = str(getattr(detail, "status", "") or "").strip()
        detail_stage = str(getattr(detail, "stage", "") or "").strip()
        if detail_status == "waiting_confirmation" or active_payload:
            return "awaiting_confirmation"
        if detail_status == "failed" or detail_stage == "offline_document_failed":
            return "failed"
        if isinstance(last_payload.get("status"), str) and str(last_payload.get("status")).strip():
            return str(last_payload.get("status")).strip()
        return detail_status or "processed"
