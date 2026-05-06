from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.services.tools.doc_tool import DocTool, DocumentSyncResult
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkflowDocExecution:
    """Builds, syncs, and packages document outputs for workflow execution."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_doc_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        reason: str,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> dict:
        workflow = self.workflow
        package, analysis = self.build_doc_response_package(
            session_id=message.session_id,
            instruction=message.text,
            llm_result=llm_result,
            workspace_context=workspace_context,
            episode_id=active_episode_id,
            reason=reason,
            source_message_id=message.message_id,
        )
        package["title"] = self.compose_doc_title(
            str(package.get("title") or "协同文档"),
            stats_as_of=str(package.get("stats_as_of") or "").strip() or None,
        )
        edit_plan = package.get("artifact_edit_plan") if isinstance(package.get("artifact_edit_plan"), dict) else {}
        logger.info(
            "Starting document sync: message_id=%s session_id=%s sections=%s has_edit_plan=%s target_document=%s",
            message.message_id,
            message.session_id,
            len(package.get("sections", [])) if isinstance(package.get("sections"), list) else 0,
            bool(edit_plan),
            bool(target_document),
        )
        sync_result = self.sync_package_to_session_doc(
            package,
            session_id=message.session_id,
            episode_id=active_episode_id,
            instruction=message.text,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        artifact = self.build_document_artifact(
            session_id=message.session_id,
            package=package,
            sync_result=sync_result,
            fallback_provider="local",
            task_run_id=task_run_id,
        )
        sync_lines = sync_result.summary_lines
        reply_preview = self.format_doc_reply(package, sync_lines)
        return {
            "reply_preview": reply_preview,
            "analysis": analysis,
            "artifacts": [artifact],
            "close_title": str(package.get("title") or "doc"),
        }

    def build_document_package_from_workspace(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        episode_id: int | None,
    ) -> dict:
        workflow = self.workflow
        stats_as_of = self.resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        provided_package = self.document_package_from_llm_result(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            episode_id=episode_id,
            stats_as_of=stats_as_of,
        )
        if provided_package:
            return provided_package

        source_text = workflow.memory_service.build_discussion_block(
            session_id,
            episode_id=episode_id,
        ) or workspace_context or instruction
        requirement_package = self.document_package_from_requirement_brief(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            source_text=source_text,
            episode_id=episode_id,
            stats_as_of=stats_as_of,
        )
        if requirement_package:
            return requirement_package
        discussion_package = workflow.document_package_builder.from_discussion_text(
            source_text,
            instruction,
            stats_as_of=stats_as_of,
        )
        if discussion_package:
            return discussion_package
        analysis = workflow._build_analysis_from_llm(
            session_id=session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent="summary",
            reason="为文档同步生成结构化沉淀",
        )
        return self.document_from_analysis(analysis, instruction, stats_as_of=stats_as_of)

    def build_doc_response_package(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        episode_id: int | None,
        reason: str,
        source_message_id: str | None,
    ) -> tuple[dict, AnalyzeResponse | None]:
        workflow = self.workflow
        provided_package = self.document_package_from_llm_result(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            episode_id=episode_id,
        )
        if provided_package:
            return provided_package, None

        source_text = workflow.memory_service.build_discussion_block(
            session_id,
            episode_id=episode_id,
            exclude_message_id=source_message_id,
        ) or workspace_context or instruction
        stats_as_of = self.resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        requirement_package = self.document_package_from_requirement_brief(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            source_text=source_text,
            episode_id=episode_id,
            stats_as_of=stats_as_of,
        )
        if requirement_package:
            return requirement_package, None
        discussion_package = workflow.document_package_builder.from_discussion_text(
            source_text,
            instruction,
            stats_as_of=stats_as_of,
        )
        if discussion_package:
            return discussion_package, None
        analysis = workflow._build_analysis_from_llm(
            session_id=session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent="summary",
            reason=reason or "为文档同步生成结构化沉淀",
        )
        workflow.memory_service.save_round(
            session_id=session_id,
            analysis=analysis,
            episode_id=episode_id,
            source_message_id=source_message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        package = self.document_from_analysis(analysis, instruction, stats_as_of=stats_as_of)
        return package, analysis

    def document_package_from_llm_result(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        episode_id: int | None,
        stats_as_of: str | None = None,
    ) -> dict | None:
        workflow = self.workflow
        resolved_stats_as_of = stats_as_of or self.resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        return workflow.document_package_builder.package_from_llm_result(
            instruction=instruction,
            llm_result=llm_result,
            stats_as_of=resolved_stats_as_of,
        )

    def document_package_from_requirement_brief(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        source_text: str,
        episode_id: int | None,
        stats_as_of: str | None = None,
    ) -> dict | None:
        workflow = self.workflow
        resolved_stats_as_of = stats_as_of or self.resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        brief_result = llm_result if isinstance(llm_result, dict) and isinstance(llm_result.get("requirement_brief"), dict) else None
        if brief_result is None:
            resolver = getattr(workflow.llm_service, "resolve_requirement_brief", None)
            if callable(resolver) and workflow.llm_service.is_configured():
                try:
                    brief_result = resolver(source_text, instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LLM requirement brief resolution failed, falling back to discussion text: %s", exc)
        brief = brief_result.get("requirement_brief") if isinstance(brief_result, dict) else None
        if not isinstance(brief, dict):
            return None
        return workflow.document_package_builder.from_requirement_brief(
            brief,
            instruction,
            stats_as_of=resolved_stats_as_of,
        )

    def is_outline_request(self, instruction: str) -> bool:
        workflow = self.workflow
        return workflow.document_package_builder.is_outline_request(instruction)

    def document_from_analysis(self, analysis: AnalyzeResponse, instruction: str, *, stats_as_of: str | None = None) -> dict:
        workflow = self.workflow
        return workflow.document_package_builder.from_analysis(analysis, instruction, stats_as_of=stats_as_of)

    def document_from_presentation(self, package: dict, instruction: str, *, stats_as_of: str | None = None) -> dict:
        workflow = self.workflow
        return workflow.document_package_builder.from_presentation(package, instruction, stats_as_of=stats_as_of)

    def normalize_doc_sections(self, sections: list[dict]) -> list[dict]:
        workflow = self.workflow
        return DocTool.normalize_doc_sections(sections)

    def sync_package_to_session_doc(
        self,
        package: dict,
        *,
        session_id: str,
        episode_id: int | None,
        instruction: str,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> DocumentSyncResult:
        workflow = self.workflow
        return workflow._doc_tool().sync_package_to_session_doc(
            package,
            session_id=session_id,
            episode_id=episode_id,
            instruction=instruction,
            task_run_id=task_run_id,
            target_document=target_document,
        )

    def build_incremental_doc_sections(
        self,
        package: dict,
        *,
        instruction: str,
        previous_snapshot: list[dict] | None = None,
    ) -> tuple[list[dict], list[str], list[str]]:
        workflow = self.workflow
        return workflow._doc_tool().build_incremental_doc_sections(
            package,
            instruction=instruction,
            previous_snapshot=previous_snapshot,
        )

    def build_doc_update_header_lines(
        self,
        *,
        instruction: str,
        targeted_headings: list[str],
    ) -> list[str]:
        workflow = self.workflow
        return DocTool.build_doc_update_header_lines(
            instruction=instruction,
            targeted_headings=targeted_headings,
        )

    def resolve_doc_update_targets(
        self,
        instruction: str,
        sections: list[dict],
    ) -> list[str]:
        workflow = self.workflow
        return DocTool.resolve_doc_update_targets(instruction, sections)

    def section_snapshot_map(self, snapshot: list[dict]) -> dict[str, list[str]]:
        workflow = self.workflow
        return DocTool.section_snapshot_map(snapshot)

    def merge_doc_section_snapshots(
        self,
        previous_snapshot: list[dict],
        updated_sections: list[dict],
        *,
        deleted_headings: list[str] | None = None,
        delete_ranges: list[dict[str, object]] | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> list[dict[str, list[str]]]:
        workflow = self.workflow
        return DocTool.merge_doc_section_snapshots(
            previous_snapshot,
            updated_sections,
            deleted_headings=deleted_headings,
            delete_ranges=delete_ranges,
            rename_map=rename_map,
        )

    def build_doc_sync_lines(
        self,
        mode: str,
        document_info: dict,
        *,
        appended_block_count: int | None = None,
        replaced_block_count: int | None = None,
        inserted_block_count: int | None = None,
        changed_headings: list[str] | None = None,
        appended_headings: list[str] | None = None,
        deleted_headings: list[str] | None = None,
        renamed_headings: list[str] | None = None,
        patched_headings: list[str] | None = None,
        unmatched_operation_targets: list[str] | None = None,
        folder_scope: str | None = None,
        folder_url: str | None = None,
        folder_note: str | None = None,
    ) -> list[str]:
        workflow = self.workflow
        return DocTool.build_doc_sync_lines(
            mode,
            document_info,
            appended_block_count=appended_block_count,
            replaced_block_count=replaced_block_count,
            inserted_block_count=inserted_block_count,
            changed_headings=changed_headings,
            appended_headings=appended_headings,
            deleted_headings=deleted_headings,
            renamed_headings=renamed_headings,
            patched_headings=patched_headings,
            unmatched_operation_targets=unmatched_operation_targets,
            folder_scope=folder_scope,
            folder_url=folder_url,
            folder_note=folder_note,
        )

    def build_document_artifact(
        self,
        *,
        session_id: str,
        package: dict,
        sync_result: DocumentSyncResult,
        fallback_provider: str,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        current_doc = sync_result.document_info or workflow.session_document_service.get_current_document(session_id) or {}
        sync_lines = sync_result.summary_lines
        if sync_result.status != "ready":
            preview = dict(package)
            try:
                local_artifact = workflow.office_artifact_service.persist_document_markdown(
                    package,
                    sync_lines=sync_lines,
                    task_run_id=task_run_id,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist local document artifact: %s", exc)
                local_artifact = {}
            previous_document = self.previous_document_preview(current_doc)
            sync_preview = self.build_document_sync_preview(
                {},
                url=local_artifact.get("url") if local_artifact else None,
                sync_lines=sync_lines,
                forced_mode=sync_result.mode,
                error=sync_result.error or self.extract_document_sync_error(sync_lines),
            )
            if previous_document:
                sync_preview["previous_document"] = previous_document
            if local_artifact:
                sync_preview["local_artifact"] = local_artifact
            sync_preview["status"] = sync_result.status
            preview["sync"] = sync_preview
            return {
                "artifact_type": "document",
                "provider": fallback_provider,
                "status": sync_result.status,
                "title": str(package.get("title") or "协同文档"),
                "url": local_artifact.get("url") if local_artifact else None,
                "preview": preview,
                "version": sync_result.version,
            }

        url = sync_result.url or workflow._extract_first_url(sync_lines)
        version = sync_result.version
        preview = dict(package)
        preview["sync"] = self.build_document_sync_preview(current_doc, url=url, sync_lines=sync_lines)
        return {
            "artifact_type": "document",
            "provider": "feishu_doc" if url else fallback_provider,
            "title": str(current_doc.get("title") or package.get("title") or "协同文档"),
            "url": url,
            "preview": preview,
            "version": version,
        }

    def build_document_sync_preview(
        self,
        document_info: dict,
        *,
        url: str | None,
        sync_lines: list[str],
        forced_mode: str | None = None,
        error: str | None = None,
    ) -> dict:
        workflow = self.workflow
        mode = str(forced_mode or document_info.get("sync_mode") or "").strip()
        if not mode:
            if any("Updated document" in line for line in sync_lines):
                mode = "updated"
            elif any("No content changes detected" in line for line in sync_lines):
                mode = "noop"
            elif any("Created document" in line for line in sync_lines):
                mode = "created"
            else:
                mode = "local_only"
        preview = {
            "mode": mode,
            "document_id": str(document_info.get("document_id") or "").strip() or None,
            "title": str(document_info.get("title") or "").strip() or None,
            "url": url,
            "version": coerce_positive_int(document_info.get("version")),
            "updated_at": str(document_info.get("updated_at") or "").strip() or None,
            "summary": [line.strip() for line in sync_lines if str(line).strip()],
        }
        if mode == "updated":
            preview["write_strategy"] = "patch_matched_section_bodies"
            preview["strategy_note"] = "Feishu Docx is updated by patching matched section bodies in place, with explicit delete and rename handling when needed."
        elif mode == "noop":
            preview["write_strategy"] = "snapshot_unchanged"
            preview["strategy_note"] = "No changed sections were detected; the existing artifact snapshot remains current."
        if error:
            preview["error"] = error
        return preview

    def is_document_sync_failed(self, sync_lines: list[str]) -> bool:
        workflow = self.workflow
        normalized = "\n".join(str(line or "").lower() for line in sync_lines)
        failure_markers = (
            "document sync failed",
            "sync failed",
            "failed:",
            "文档创建失败",
            "同步失败",
            "未启用",
            "disabled",
            "skipped",
        )
        return any(marker in normalized for marker in failure_markers)

    def extract_document_sync_error(self, sync_lines: list[str]) -> str | None:
        workflow = self.workflow
        for line in sync_lines:
            text = str(line or "").strip().lstrip("-").strip()
            lowered = text.lower()
            if any(marker in lowered for marker in ("failed", "失败", "disabled", "skipped", "未启用")):
                return text
        return None

    def previous_document_preview(self, current_doc: dict) -> dict | None:
        workflow = self.workflow
        if not isinstance(current_doc, dict) or not current_doc.get("document_id"):
            return None
        return {
            "document_id": str(current_doc.get("document_id") or "").strip() or None,
            "title": str(current_doc.get("title") or "").strip() or None,
            "url": str(current_doc.get("url") or "").strip() or None,
            "version": coerce_positive_int(current_doc.get("version")),
            "updated_at": str(current_doc.get("updated_at") or "").strip() or None,
        }

    def sync_package_to_doc(self, package: dict) -> list[str]:
        workflow = self.workflow
        if not workflow.doc_api.is_configured():
            logger.warning("Feishu doc sync skipped because FEISHU_DOC_ENABLED is not enabled.")
            return ["- 飞书文档未启用，请先在环境变量里设置 FEISHU_DOC_ENABLED=true。"]

        try:
            created = workflow.doc_api.create_document_from_sections(
                str(package.get("title") or "协同文档"),
                package.get("sections") if isinstance(package.get("sections"), list) else [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu doc sync failed: %s", exc)
            return [f"- 文档创建失败：{exc}"]

        lines = [f"- 已创建飞书文档：《{created['title']}》"]
        if created.get("url"):
            lines.append(f"- 文档链接：{created['url']}")
        folder_scope = str(created.get("folder_scope") or "").strip()
        if folder_scope == "explicit" and created.get("folder_url"):
            lines.append(f"- 产出目录：{created['folder_url']}")
        folder_note = str(created.get("folder_note") or "").strip()
        if folder_note:
            lines.append(f"- {folder_note}")
        return lines

    def format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        workflow = self.workflow
        return workflow.response_formatter.format_doc_reply(package, sync_lines)

    def default_doc_title(self, instruction: str, fallback: str | None = None, stats_as_of: str | None = None) -> str:
        workflow = self.workflow
        timestamp = stats_as_of or self.doc_title_timestamp()
        if fallback:
            return self.compose_doc_title(f"{settings.feishu_doc_title_prefix} - {fallback.strip()}", stats_as_of=timestamp)
        condensed = " ".join((instruction or "").split()).strip()
        if condensed:
            condensed = condensed[:24]
            return self.compose_doc_title(f"{settings.feishu_doc_title_prefix} - {condensed}", stats_as_of=timestamp)
        return self.compose_doc_title(f"{settings.feishu_doc_title_prefix} - 讨论整理", stats_as_of=timestamp)

    def doc_title_timestamp(self) -> str:
        workflow = self.workflow
        return workflow.document_package_builder.title_timestamp()

    def resolve_doc_stats_as_of(self, session_id: str, *, episode_id: int | None) -> str | None:
        workflow = self.workflow
        cutoff_at = workflow.memory_service.get_discussion_cutoff_at(session_id, episode_id=episode_id)
        if cutoff_at is None:
            return None
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=ZoneInfo("UTC"))
        return cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    def compose_doc_title(self, base_title: str, *, stats_as_of: str | None) -> str:
        workflow = self.workflow
        return workflow.document_package_builder.compose_title(base_title, stats_as_of=stats_as_of)
