from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.feishu.doc_api import FeishuDocAPI
from app.feishu.import_api import FeishuImportAPI
from app.feishu.media_api import FeishuMediaAPI
from app.services.session_document_service import SessionDocumentService

logger = logging.getLogger(__name__)


class FeishuArtifactIntegrator:
    """Sync delivery artifacts into a Feishu Docx overview document."""

    def __init__(
        self,
        *,
        doc_api: FeishuDocAPI | None = None,
        media_api: FeishuMediaAPI | None = None,
        import_api: FeishuImportAPI | None = None,
        session_document_service: SessionDocumentService | None = None,
        requirement_service: Any | None = None,
    ) -> None:
        self.doc_api = doc_api or FeishuDocAPI()
        self.media_api = media_api or FeishuMediaAPI()
        self.import_api = import_api or FeishuImportAPI()
        self.session_document_service = session_document_service or SessionDocumentService()
        self.requirement_service = requirement_service

    def is_enabled(self) -> bool:
        return bool(
            settings.feishu_artifact_sync_enabled
            and settings.feishu_artifact_sync_on_delivery
            and self.doc_api.is_configured()
        )

    def sync_delivery_manifest(self, detail: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        if not settings.feishu_artifact_sync_enabled:
            return self._skipped("feishu_artifact_sync_disabled")
        if not settings.feishu_artifact_sync_on_delivery:
            return self._skipped("feishu_artifact_sync_on_delivery_disabled")
        if not self.doc_api.is_configured():
            return self._skipped("feishu_doc_not_configured")

        session_id = str(getattr(detail, "session_id", "") or manifest.get("session_id") or "").strip()
        task_run_id = str(getattr(detail, "task_run_id", "") or manifest.get("task_run_id") or "").strip()
        if not session_id:
            return self._skipped("session_id_missing")

        title = self._document_title(detail, manifest)
        sections = self._sections(detail, manifest)
        target = self._target_document(detail, manifest)

        try:
            if target.get("document_id"):
                document_id = str(target.get("document_id") or "").strip()
                result = self.doc_api.append_sections_to_document(document_id, title, sections)
                sync_mode = "delivery_updated"
                version = self._next_version(target.get("version"))
                section_snapshot = self._section_snapshot_for_save(target, sections, append=True)
                section_block_index = (
                    target.get("section_block_index")
                    if isinstance(target.get("section_block_index"), list)
                    else []
                )
            else:
                result = self.doc_api.create_document_from_sections(title, sections)
                sync_mode = "delivery_created"
                version = 1
                section_snapshot = sections
                section_block_index = (
                    result.get("section_block_index")
                    if isinstance(result.get("section_block_index"), list)
                    else []
                )

            document_id = str(result.get("document_id") or target.get("document_id") or "").strip()
            url = str(result.get("url") or target.get("url") or "").strip()
            media_results = [
                self._sync_canvas_image_to_document(document_id, manifest, title=title),
                self._sync_slides_file_to_document(document_id, manifest, title=title),
            ]
            status = "ready"
            warnings = []
            media_items = []
            media_sections = []
            for media_sync in media_results:
                if media_sync.get("warnings"):
                    warnings.extend(media_sync["warnings"])
                    status = "partial"
                if isinstance(media_sync.get("items"), list):
                    media_items.extend(media_sync["items"])
                if isinstance(media_sync.get("sections"), list):
                    media_sections.extend(item for item in media_sync["sections"] if isinstance(item, dict))
            if media_sections:
                section_snapshot = [item for item in section_snapshot if isinstance(item, dict)] + media_sections
            saved = self.session_document_service.save_current_document(
                session_id,
                document_id=document_id,
                url=url or None,
                title=str(result.get("title") or title),
                task_run_id=task_run_id or None,
                version=version,
                sync_mode=sync_mode,
                section_snapshot=section_snapshot,
                section_block_index=section_block_index,
            )
            document_url = url or saved.get("url")
            synced_items = self._synced_items(manifest)
            artifact_syncs = self._artifact_syncs(
                synced_items,
                media_items,
                document_id=document_id,
                document_url=str(document_url or ""),
                synced_at=datetime.now(timezone.utc).isoformat(),
                sync_mode=sync_mode,
                warnings=warnings,
            )
            return {
                "status": status,
                "sync_mode": sync_mode,
                "scope": "requirement" if str(manifest.get("requirement_id") or "").strip() else "session",
                "requirement_id": str(manifest.get("requirement_id") or "").strip() or None,
                "task_run_id": task_run_id or None,
                "session_id": session_id,
                "document_id": document_id,
                "url": document_url,
                "document_url": document_url,
                "title": str(result.get("title") or title),
                "synced_at": artifact_syncs.get("synced_at"),
                "synced_artifact_count": self._ready_deliverable_count(manifest),
                "items": synced_items,
                "media_items": media_items,
                "artifact_syncs": artifact_syncs.get("by_kind", {}),
                "document_sync": artifact_syncs.get("by_kind", {}).get("document"),
                "slides_sync": artifact_syncs.get("by_kind", {}).get("slides"),
                "canvas_sync": artifact_syncs.get("by_kind", {}).get("canvas"),
                "warnings": warnings,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to sync delivery artifacts to Feishu doc: task_run_id=%s session_id=%s error=%s",
                task_run_id,
                session_id,
                exc,
            )
            return {
                "status": "failed",
                "sync_mode": "failed",
                "document_id": str(target.get("document_id") or "").strip() or None,
                "url": str(target.get("url") or "").strip() or None,
                "document_url": str(target.get("url") or "").strip() or None,
                "title": title,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "synced_artifact_count": 0,
                "items": [],
                "warnings": [str(exc)],
            }

    def sync_current_task_run_artifacts(self, detail: Any) -> dict[str, Any]:
        if not settings.feishu_artifact_sync_enabled:
            return self._skipped("feishu_artifact_sync_disabled")
        if not self.doc_api.is_configured():
            return self._skipped("feishu_doc_not_configured")

        target = self._target_document(detail, {})
        title = str(target.get("title") or getattr(detail, "title", "") or "AI 协作文档").strip()[:120] or "AI 协作文档"
        session_id = str(getattr(detail, "session_id", "") or target.get("session_id") or "").strip()
        task_run_id = str(getattr(detail, "task_run_id", "") or "").strip()

        slides_artifact = self._latest_artifact_by_types(
            getattr(detail, "artifacts", None),
            {"slides", "slides_package"},
        )

        canvas_artifact = self._latest_artifact_by_types(
            getattr(detail, "artifacts", None),
            {"canvas"},
        )
        return self._sync_current_artifact_sections(
            target=target,
            title=title,
            session_id=session_id,
            task_run_id=task_run_id or None,
            slides_artifact=slides_artifact,
            canvas_artifact=canvas_artifact,
            failure_log_label="task-run",
        )

    def sync_requirement_current_artifacts(self, requirement: Any) -> dict[str, Any]:
        if not settings.feishu_artifact_sync_enabled:
            return self._skipped("feishu_artifact_sync_disabled")
        if not self.doc_api.is_configured():
            return self._skipped("feishu_doc_not_configured")

        target = self._as_dict(getattr(requirement, "current_document", None))
        title = str(target.get("title") or getattr(requirement, "title", "") or "AI 协作文档").strip()[:120] or "AI 协作文档"
        session_id = str(target.get("session_id") or getattr(requirement, "primary_session_id", "") or "").strip()
        slides_artifact = self._as_dict(getattr(requirement, "current_slides", None)) or None
        canvas_artifact = self._as_dict(getattr(requirement, "current_canvas", None)) or None
        return self._sync_current_artifact_sections(
            target=target,
            title=title,
            session_id=session_id,
            task_run_id=None,
            slides_artifact=slides_artifact,
            canvas_artifact=canvas_artifact,
            failure_log_label="requirement",
        )

    def _sync_current_artifact_sections(
        self,
        *,
        target: dict[str, Any],
        title: str,
        session_id: str,
        task_run_id: str | None,
        slides_artifact: dict[str, Any] | None,
        canvas_artifact: dict[str, Any] | None,
        failure_log_label: str,
    ) -> dict[str, Any]:
        document_id = str(target.get("document_id") or "").strip()
        if not document_id:
            return self._skipped("current_document_missing")

        sections: list[dict[str, Any]] = []
        target_headings: list[str] = []
        artifact_syncs: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []

        if slides_artifact:
            slides_sync = self._build_current_slides_section(document_id, slides_artifact)
            warnings.extend(slides_sync.get("warnings", []))
            section = slides_sync.get("section")
            if isinstance(section, dict):
                sections.append(section)
                target_headings.append(str(section.get("heading") or "").strip())
            artifact_syncs["slides"] = slides_sync

        if canvas_artifact:
            canvas_sync = self._build_current_canvas_section(document_id, canvas_artifact)
            warnings.extend(canvas_sync.get("warnings", []))
            section = canvas_sync.get("section")
            if isinstance(section, dict):
                sections.append(section)
                target_headings.append(str(section.get("heading") or "").strip())
            artifact_syncs["canvas"] = canvas_sync

        target_headings = [heading for heading in target_headings if heading]
        if not sections or not target_headings:
            return self._skipped("current_artifacts_missing")

        try:
            result = self.doc_api.replace_document_sections(
                document_id,
                title,
                sections,
                target_headings=target_headings,
                append_headings=target_headings,
            )
            version = self._next_version(target.get("version"))
            saved = self.session_document_service.save_current_document(
                session_id,
                document_id=document_id,
                url=str(result.get("url") or target.get("url") or "").strip() or None,
                title=str(result.get("title") or title),
                task_run_id=task_run_id,
                version=version,
                sync_mode="current_artifacts_updated",
                section_snapshot=(
                    result.get("section_snapshot")
                    if isinstance(result.get("section_snapshot"), list)
                    else target.get("section_snapshot")
                ),
                section_block_index=(
                    result.get("section_block_index")
                    if isinstance(result.get("section_block_index"), list)
                    else target.get("section_block_index")
                ),
            )
            document_url = str(result.get("url") or saved.get("url") or target.get("url") or "").strip() or None
            synced_at = datetime.now(timezone.utc).isoformat()
            by_kind = {
                kind: self._current_artifact_sync_payload(
                    kind,
                    sync,
                    document_id=document_id,
                    document_url=document_url,
                    synced_at=synced_at,
                )
                for kind, sync in artifact_syncs.items()
                if isinstance(sync, dict)
            }
            return {
                "status": self._aggregate_sync_status([payload.get("status") for payload in artifact_syncs.values()]),
                "sync_mode": "current_artifacts_updated",
                "document_id": document_id,
                "url": document_url,
                "document_url": document_url,
                "title": str(result.get("title") or title),
                "task_run_id": task_run_id,
                "session_id": session_id or None,
                "artifact_syncs": by_kind,
                "slides_sync": by_kind.get("slides"),
                "canvas_sync": by_kind.get("canvas"),
                "items": [item for payload in by_kind.values() for item in payload.get("items", []) if isinstance(item, dict)],
                "warnings": warnings,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to sync current %s artifacts to Feishu doc: task_run_id=%s document_id=%s error=%s",
                failure_log_label,
                task_run_id,
                document_id,
                exc,
            )
            return {
                "status": "failed",
                "sync_mode": "failed",
                "document_id": document_id,
                "url": str(target.get("url") or "").strip() or None,
                "document_url": str(target.get("url") or "").strip() or None,
                "title": title,
                "task_run_id": task_run_id,
                "session_id": session_id or None,
                "artifact_syncs": {},
                "items": [],
                "warnings": [str(exc)],
            }

    def _target_document(self, detail: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        requirement_id = str(getattr(detail, "requirement_id", "") or manifest.get("requirement_id") or "").strip()
        if requirement_id and self.requirement_service is not None:
            try:
                requirement = self.requirement_service.get_requirement(requirement_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to load requirement delivery document target: requirement_id=%s error=%s", requirement_id, exc)
                requirement = None
            current_document = getattr(requirement, "current_document", None) if requirement is not None else None
            payload = self._as_dict(current_document)
            if payload.get("document_id"):
                return payload

        documents = getattr(detail, "session_documents", None)
        if isinstance(documents, list):
            document_payloads = [self._as_dict(item) for item in documents]
            current = [item for item in document_payloads if item.get("is_current")]
            candidates = current or document_payloads
            candidates = [item for item in candidates if item.get("document_id")]
            if candidates:
                return candidates[0]

        if requirement_id:
            return {}

        session_id = str(getattr(detail, "session_id", "") or manifest.get("session_id") or "").strip()
        if session_id:
            try:
                current_document = self.session_document_service.get_current_document(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to load session delivery document target: session_id=%s error=%s", session_id, exc)
                current_document = None
            payload = self._as_dict(current_document)
            if payload.get("document_id"):
                return payload
        return {}

    def _sync_canvas_image_to_document(self, document_id: str, manifest: dict[str, Any], *, title: str) -> dict[str, Any]:
        if not settings.feishu_artifact_canvas_image_enabled:
            return {"status": "skipped", "items": [], "warnings": []}
        canvas = self._deliverable_by_key(
            [item for item in manifest.get("deliverables", []) if isinstance(item, dict)],
            "canvas",
        )
        if not canvas or canvas.get("status") != "ready":
            return {"status": "skipped", "items": [], "warnings": []}
        png_path = self._local_artifact_path_for_link(canvas, preferred_label="PNG", kind="canvas", allow_fallback=False)
        svg_path = self._local_artifact_path_for_link(canvas, preferred_label="SVG", kind="canvas", allow_fallback=False)
        if png_path is None and svg_path is not None:
            derived_png = svg_path.with_suffix(".png")
            if derived_png.is_file():
                png_path = derived_png
        if png_path is None and svg_path is None:
            return {
                "status": "skipped",
                "items": [],
                "warnings": ["canvas_preview_file_not_found"],
            }
        image_path = png_path
        try:
            if image_path is None:
                raise RuntimeError("canvas_png_file_not_found")
            upload = self.media_api.upload_docx_image(
                document_id=document_id,
                file_path=image_path,
                file_name=image_path.name,
            )
            file_token = str(upload.get("file_token") or upload.get("token") or "").strip()
            if not file_token:
                raise RuntimeError("Feishu image upload did not return a token.")
            image_section = {
                "heading": "Canvas 图片预览",
                "paragraphs": [
                    {
                        "type": "image",
                        "token": file_token,
                        "bind_after_create": True,
                        "caption": str(canvas.get("title") or canvas.get("label") or "Canvas"),
                    }
                ],
            }
            self.doc_api.append_sections_to_document(
                document_id,
                title,
                [image_section],
            )
            return {
                "status": "ready",
                "items": [
                    {
                        "kind": "canvas_image",
                        "status": "ready",
                        "file_token": file_token,
                        "source_path": str(image_path),
                        "source_url": self._first_link_url(canvas, preferred_label="PNG", allow_fallback=True),
                    }
                ],
                "sections": [image_section],
                "warnings": [],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to upload Canvas image to Feishu doc: document_id=%s error=%s", document_id, exc)
            link_section = {
                "heading": "Canvas 预览链接",
                "paragraphs": self._deliverable_paragraphs(canvas),
            }
            self.doc_api.append_sections_to_document(
                document_id,
                title,
                [link_section],
            )
            return {
                "status": "partial",
                "items": [
                    {
                        "kind": "canvas_link",
                        "status": "ready",
                        "source_path": str(svg_path or image_path or ""),
                        "source_url": self._first_link_url(canvas, preferred_label="HTML", allow_fallback=True),
                    }
                ],
                "sections": [link_section],
                "warnings": [f"canvas_image_upload_failed: {exc}"],
            }

    def _sync_slides_file_to_document(self, document_id: str, manifest: dict[str, Any], *, title: str) -> dict[str, Any]:
        if not (settings.feishu_artifact_slides_upload_enabled or settings.feishu_artifact_slides_import_enabled):
            return {"status": "skipped", "items": [], "warnings": []}
        slides = self._deliverable_by_key(
            [item for item in manifest.get("deliverables", []) if isinstance(item, dict)],
            "slides",
        )
        if not slides or slides.get("status") != "ready":
            return {"status": "skipped", "items": [], "warnings": []}
        pptx_path = self._local_artifact_path_for_link(slides, preferred_label="PPTX", kind="slides", allow_fallback=False)
        if pptx_path is None:
            return {
                "status": "skipped",
                "items": [],
                "warnings": ["slides_pptx_file_not_found"],
            }
        source_url = self._first_link_url(slides, preferred_label="PPTX", allow_fallback=False)
        items: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        warnings: list[str] = []
        if settings.feishu_artifact_slides_upload_enabled:
            try:
                upload = self.media_api.upload_docx_file(
                    document_id=document_id,
                    file_path=pptx_path,
                    file_name=pptx_path.name,
                )
                file_token = str(upload.get("file_token") or upload.get("token") or "").strip()
                if not file_token:
                    raise RuntimeError("Feishu file upload did not return a token.")
                items.append(
                    {
                        "kind": "slides_pptx",
                        "status": "ready",
                        "file_token": file_token,
                        "source_path": str(pptx_path),
                        "source_url": source_url,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to upload PPTX to Feishu doc: document_id=%s error=%s", document_id, exc)
                warnings.append(f"slides_pptx_upload_failed: {exc}")
            else:
                file_section = {
                    "heading": "PPTX 飞书附件",
                    "paragraphs": [
                        f"已上传 PPTX: {pptx_path.name}",
                        f"File token: {file_token}",
                        f"本地预览链接: {source_url}",
                    ],
                }
                sections.append(file_section)
        import_sync = self._import_slides_to_cloud_doc(
            document_id,
            pptx_path,
            slides,
            source_url=source_url,
        )
        warnings.extend(str(warning) for warning in import_sync.get("warnings", []) if str(warning).strip())
        items.extend(item for item in import_sync.get("items", []) if isinstance(item, dict))
        sections.extend(section for section in import_sync.get("sections", []) if isinstance(section, dict))
        if sections:
            self.doc_api.append_sections_to_document(
                document_id,
                title,
                sections,
            )
        ready_items = [item for item in items if str(item.get("status") or "").strip().lower() == "ready"]
        if warnings and not ready_items:
            status = "failed"
        elif warnings:
            status = "partial"
        elif ready_items:
            status = "ready"
        else:
            status = "skipped"
        return {
            "status": status,
            "items": items,
            "sections": sections,
            "warnings": warnings,
        }

    def _build_current_slides_section(self, document_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        slides = self._artifact_as_deliverable(artifact)
        if not slides or slides.get("status") != "ready":
            return {"status": "skipped", "items": [], "media_items": [], "warnings": []}

        source_url = self._first_link_url(slides, preferred_label="HTML", allow_fallback=True)
        paragraphs = self._deliverable_paragraphs(slides)
        media_items: list[dict[str, Any]] = []
        warnings: list[str] = []

        pptx_path = self._local_artifact_path_for_link(slides, preferred_label="PPTX", kind="slides", allow_fallback=False)
        if (settings.feishu_artifact_slides_upload_enabled or settings.feishu_artifact_slides_import_enabled) and pptx_path is None:
            warnings.append("slides_pptx_file_not_found")

        if settings.feishu_artifact_slides_upload_enabled and pptx_path is not None:
            try:
                upload = self.media_api.upload_docx_file(
                    document_id=document_id,
                    file_path=pptx_path,
                    file_name=pptx_path.name,
                )
                file_token = str(upload.get("file_token") or upload.get("token") or "").strip()
                if not file_token:
                    raise RuntimeError("Feishu file upload did not return a token.")
                item = {
                    "kind": "slides_pptx",
                    "status": "ready",
                    "file_token": file_token,
                    "source_path": str(pptx_path),
                    "source_url": self._first_link_url(slides, preferred_label="PPTX", allow_fallback=False),
                }
                media_items.append(item)
                paragraphs.extend(
                    [
                        f"PPTX 附件已上传: {pptx_path.name}",
                        f"File token: {file_token}",
                    ]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to upload PPTX to Feishu doc: document_id=%s error=%s", document_id, exc)
                warnings.append(f"slides_pptx_upload_failed: {exc}")

        if settings.feishu_artifact_slides_import_enabled and pptx_path is not None:
            import_sync = self._import_slides_to_cloud_doc(
                document_id,
                pptx_path,
                slides,
                source_url=self._first_link_url(slides, preferred_label="PPTX", allow_fallback=False),
            )
            warnings.extend(str(item) for item in import_sync.get("warnings", []) if str(item).strip())
            for item in import_sync.get("items", []):
                if isinstance(item, dict):
                    media_items.append(item)
                    if str(item.get("status") or "").strip().lower() == "ready":
                        cloud_url = str(item.get("cloud_url") or item.get("cloud_token") or "").strip()
                        ticket = str(item.get("ticket") or "").strip()
                        if cloud_url:
                            paragraphs.append(f"飞书云文档: {cloud_url}")
                        if ticket:
                            paragraphs.append(f"Import ticket: {ticket}")

        status = self._aggregate_sync_status(
            [
                "ready",
                "partial" if warnings and media_items else "",
                "failed" if warnings and not media_items and (settings.feishu_artifact_slides_upload_enabled or settings.feishu_artifact_slides_import_enabled) else "",
            ]
        )
        return {
            "status": status,
            "source_url": source_url,
            "items": [
                {
                    "kind": "slides",
                    "status": "linked",
                    "source_url": source_url,
                }
            ],
            "media_items": media_items,
            "section": {
                "heading": "当前 PPT",
                "paragraphs": paragraphs,
            },
            "warnings": warnings,
        }

    def _build_current_canvas_section(self, document_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
        canvas = self._artifact_as_deliverable(artifact)
        if not canvas or canvas.get("status") != "ready":
            return {"status": "skipped", "items": [], "media_items": [], "warnings": []}

        source_url = self._first_link_url(canvas, preferred_label="HTML", allow_fallback=True)
        paragraphs = self._deliverable_paragraphs(canvas)
        section_paragraphs: list[Any] = list(paragraphs)
        media_items: list[dict[str, Any]] = []
        warnings: list[str] = []

        if settings.feishu_artifact_canvas_image_enabled:
            png_path = self._local_artifact_path_for_export(canvas, export_key="png", kind="canvas")
            if png_path is None:
                png_path = self._local_artifact_path_for_link(canvas, preferred_label="PNG", kind="canvas", allow_fallback=False)
            svg_path = self._local_artifact_path_for_export(canvas, export_key="svg", kind="canvas")
            if svg_path is None:
                svg_path = self._local_artifact_path_for_link(canvas, preferred_label="SVG", kind="canvas", allow_fallback=False)
            if png_path is None and svg_path is not None:
                derived_png = svg_path.with_suffix(".png")
                if derived_png.is_file():
                    png_path = derived_png
            if png_path is None and svg_path is None:
                warnings.append("canvas_preview_file_not_found")
            else:
                try:
                    if png_path is None:
                        raise RuntimeError("canvas_png_file_not_found")
                    upload = self.media_api.upload_docx_image(
                        document_id=document_id,
                        file_path=png_path,
                        file_name=png_path.name,
                    )
                    file_token = str(upload.get("file_token") or upload.get("token") or "").strip()
                    if not file_token:
                        raise RuntimeError("Feishu image upload did not return a token.")
                    media_items.append(
                        {
                            "kind": "canvas_image",
                            "status": "ready",
                            "file_token": file_token,
                            "source_path": str(png_path),
                            "source_url": self._first_link_url(canvas, preferred_label="PNG", allow_fallback=True),
                        }
                    )
                    section_paragraphs = [
                        *paragraphs[:2],
                        {
                            "type": "image",
                            "token": file_token,
                            "bind_after_create": True,
                            "caption": str(canvas.get("title") or canvas.get("label") or "Canvas"),
                        },
                        *paragraphs[2:],
                    ]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to upload Canvas image to Feishu doc: document_id=%s error=%s", document_id, exc)
                    warnings.append(f"canvas_image_upload_failed: {exc}")
                    media_items.append(
                        {
                            "kind": "canvas_link",
                            "status": "ready",
                            "source_path": str(svg_path or png_path or ""),
                            "source_url": source_url,
                        }
                    )

        status = self._aggregate_sync_status(
            [
                "ready",
                "partial" if warnings and media_items else "",
                "failed" if warnings and not media_items and settings.feishu_artifact_canvas_image_enabled else "",
            ]
        )
        return {
            "status": status,
            "source_url": source_url,
            "items": [
                {
                    "kind": "canvas",
                    "status": "linked",
                    "source_url": source_url,
                }
            ],
            "media_items": media_items,
            "section": {
                "heading": "当前 Canvas",
                "paragraphs": section_paragraphs,
            },
            "warnings": warnings,
        }

    def _import_slides_to_cloud_doc(
        self,
        document_id: str,
        pptx_path: Path,
        slides: dict[str, Any],
        *,
        source_url: str,
    ) -> dict[str, Any]:
        if not settings.feishu_artifact_slides_import_enabled:
            return {"status": "skipped", "items": [], "sections": [], "warnings": []}
        import_type = str(settings.feishu_artifact_slides_import_type or "slides").strip() or "slides"
        try:
            upload = self.media_api.upload_import_file(
                file_path=pptx_path,
                file_name=pptx_path.name,
                file_extension="pptx",
                obj_type=import_type,
            )
            import_file_token = str(upload.get("file_token") or upload.get("token") or "").strip()
            if not import_file_token:
                raise RuntimeError("Feishu import upload did not return a file token.")
            created = self.import_api.create_import_task(
                file_token=import_file_token,
                file_extension="pptx",
                import_type=import_type,
                file_name=pptx_path.name,
            )
            ticket = str(created.get("ticket") or "").strip()
            if not ticket:
                raise RuntimeError("Feishu import task creation did not return a ticket.")
            imported = self.import_api.wait_for_import(
                ticket,
                timeout_seconds=settings.feishu_artifact_slides_import_timeout_seconds,
                poll_seconds=settings.feishu_artifact_slides_import_poll_seconds,
            )
            if str(imported.get("status") or "").strip().lower() != "ready":
                raise RuntimeError(f"Feishu import task failed: status={imported.get('raw_status') or imported.get('status')}")
            cloud_token = str(imported.get("token") or "").strip()
            cloud_url = str(imported.get("url") or "").strip()
            if not cloud_token and not cloud_url:
                raise RuntimeError("Feishu import task completed without a cloud document token or URL.")
            item = {
                "kind": "slides_import",
                "status": "ready",
                "file_token": import_file_token,
                "ticket": str(imported.get("ticket") or ticket),
                "cloud_token": cloud_token,
                "cloud_url": cloud_url,
                "source_path": str(pptx_path),
                "source_url": source_url or self._first_link_url(slides, preferred_label="PPTX", allow_fallback=False),
            }
            section = {
                "heading": "PPT 飞书云文档",
                "paragraphs": [
                    f"已导入 PPTX: {pptx_path.name}",
                    f"飞书云文档: {cloud_url or cloud_token or 'N/A'}",
                    f"Import ticket: {ticket}",
                ],
            }
            return {
                "status": "ready",
                "items": [item],
                "sections": [section],
                "warnings": [],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to import PPTX to Feishu cloud doc: document_id=%s error=%s", document_id, exc)
            return {
                "status": "failed",
                "items": [
                    {
                        "kind": "slides_import",
                        "status": "failed",
                        "source_path": str(pptx_path),
                        "source_url": source_url,
                        "error": str(exc),
                    }
                ],
                "sections": [],
                "warnings": [f"slides_import_failed: {exc}"],
            }

    def _document_title(self, detail: Any, manifest: dict[str, Any]) -> str:
        prefix = str(settings.feishu_artifact_delivery_doc_title_prefix or "").strip() or "AI协作交付包"
        base = str(manifest.get("title") or getattr(detail, "title", "") or getattr(detail, "task_run_id", "") or "").strip()
        if base.startswith(prefix):
            return base[:120]
        return f"{prefix} - {base or '未命名需求'}"[:120]

    def _sections(self, detail: Any, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        deliverables = [item for item in manifest.get("deliverables", []) if isinstance(item, dict)]
        sections: list[dict[str, Any]] = [
            {
                "heading": "交付总览",
                "paragraphs": [
                    str(manifest.get("summary") or "本次交付包已生成。").strip(),
                    f"Task Run: {getattr(detail, 'task_run_id', '') or manifest.get('task_run_id', '')}",
                    f"Requirement: {getattr(detail, 'requirement_id', '') or manifest.get('requirement_id', '') or 'N/A'}",
                    f"Synced at: {datetime.now(timezone.utc).isoformat()}",
                ],
            },
            {
                "heading": "最新产物链接",
                "paragraphs": [
                    {
                        "type": "table",
                        "rows": self._deliverable_rows(deliverables),
                    }
                ],
            },
        ]
        for key, heading in (("canvas", "Canvas 预览"), ("slides", "PPT 演示稿"), ("document", "需求文档")):
            item = self._deliverable_by_key(deliverables, key)
            if item:
                sections.append({"heading": heading, "paragraphs": self._deliverable_paragraphs(item)})

        checks = self._check_lines(manifest)
        if checks:
            sections.append({"heading": "验收清单", "paragraphs": checks})
        next_steps = [f"- {item}" for item in manifest.get("next_steps", []) if str(item).strip()]
        if next_steps:
            sections.append({"heading": "下一步建议", "paragraphs": next_steps})
        return sections

    def _deliverable_rows(self, deliverables: list[dict[str, Any]]) -> list[list[str]]:
        rows = [["产物", "状态", "主链接", "导出链接"]]
        for item in deliverables:
            links = self._normalized_links(item)
            primary = self._absolute_url(str(item.get("url") or "").strip()) or (links[0]["url"] if links else "")
            extra = "; ".join(f"{link['label']}: {link['url']}" for link in links[:5])
            rows.append(
                [
                    str(item.get("label") or item.get("artifact_type") or item.get("key") or "产物"),
                    self._status_label(str(item.get("status") or "")),
                    primary or "N/A",
                    extra or "N/A",
                ]
            )
        return rows

    def _deliverable_paragraphs(self, item: dict[str, Any]) -> list[str]:
        paragraphs = [
            f"标题: {item.get('title') or item.get('label') or item.get('artifact_type') or '未命名产物'}",
            f"状态: {self._status_label(str(item.get('status') or ''))}",
        ]
        detail = str(item.get("detail") or "").strip()
        if detail:
            paragraphs.append(f"说明: {detail}")
        for link in self._normalized_links(item):
            paragraphs.append(f"- {link['label']}: {link['url']}")
        return paragraphs

    def _normalized_links(self, item: dict[str, Any]) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        primary = self._absolute_url(str(item.get("url") or "").strip())
        if primary:
            links.append({"label": "打开", "url": primary})
        raw_links = item.get("links") if isinstance(item.get("links"), list) else []
        for raw in raw_links:
            if not isinstance(raw, dict):
                continue
            url = self._absolute_url(str(raw.get("url") or "").strip())
            if not url or any(existing["url"] == url for existing in links):
                continue
            links.append({"label": str(raw.get("label") or "链接").strip() or "链接", "url": url})
        return links

    def _local_artifact_path_for_link(
        self,
        item: dict[str, Any],
        *,
        preferred_label: str,
        kind: str,
        allow_fallback: bool = True,
    ) -> Path | None:
        url = self._first_link_url(item, preferred_label=preferred_label, allow_fallback=allow_fallback)
        if not url:
            return None
        prefix = f"/api/artifacts/{kind}/"
        if url.startswith(("http://", "https://")):
            base = str(settings.artifact_public_base_url or "").strip().rstrip("/")
            if base and url.startswith(f"{base}{prefix}"):
                filename = url.removeprefix(f"{base}{prefix}")
            else:
                return None
        elif url.startswith(prefix):
            filename = url.removeprefix(prefix)
        else:
            return None
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            return None
        path = Path("data") / "artifacts" / kind / filename
        return path if path.is_file() else None

    def _local_artifact_path_for_export(self, item: dict[str, Any], *, export_key: str, kind: str) -> Path | None:
        exports = item.get("preview_exports") if isinstance(item.get("preview_exports"), dict) else {}
        url = self._absolute_url(str(exports.get(export_key) or "").strip())
        if not url:
            return None
        prefix = f"/api/artifacts/{kind}/"
        if url.startswith(("http://", "https://")):
            base = str(settings.artifact_public_base_url or "").strip().rstrip("/")
            if base and url.startswith(f"{base}{prefix}"):
                filename = url.removeprefix(f"{base}{prefix}")
            else:
                return None
        elif url.startswith(prefix):
            filename = url.removeprefix(prefix)
        else:
            return None
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            return None
        path = Path("data") / "artifacts" / kind / filename
        return path if path.is_file() else None

    def _first_link_url(self, item: dict[str, Any], *, preferred_label: str, allow_fallback: bool = True) -> str:
        preferred = preferred_label.strip().lower()
        links = self._normalized_links(item)
        for link in links:
            if str(link.get("label") or "").strip().lower() == preferred:
                return str(link.get("url") or "").strip()
        if not allow_fallback:
            return ""
        for link in links:
            url = str(link.get("url") or "").strip()
            if url:
                return url
        return ""

    def _synced_items(self, manifest: dict[str, Any]) -> list[dict[str, str]]:
        items = []
        for item in manifest.get("deliverables", []):
            if not isinstance(item, dict) or item.get("status") != "ready":
                continue
            key = str(item.get("key") or item.get("artifact_type") or "artifact")
            links = self._normalized_links(item)
            items.append(
                {
                    "kind": key,
                    "status": "linked",
                    "source_url": links[0]["url"] if links else "",
                }
            )
        return items

    def _artifact_syncs(
        self,
        items: list[dict[str, Any]],
        media_items: list[dict[str, Any]],
        *,
        document_id: str,
        document_url: str,
        synced_at: str,
        sync_mode: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        by_kind: dict[str, dict[str, Any]] = {}
        for item in items:
            kind = str(item.get("kind") or "").strip()
            if not kind:
                continue
            by_kind[kind] = {
                "kind": kind,
                "status": "linked",
                "document_id": document_id,
                "document_url": document_url,
                "synced_at": synced_at,
                "sync_mode": sync_mode,
                "source_url": item.get("source_url"),
                "items": [item],
                "media_items": [],
                "warnings": list(warnings),
            }
        for media_item in media_items:
            if not isinstance(media_item, dict):
                continue
            owner_kind = self._media_owner_kind(str(media_item.get("kind") or ""))
            if not owner_kind:
                continue
            payload = by_kind.setdefault(
                owner_kind,
                {
                    "kind": owner_kind,
                    "status": "linked",
                    "document_id": document_id,
                    "document_url": document_url,
                    "synced_at": synced_at,
                    "sync_mode": sync_mode,
                    "source_url": "",
                    "items": [],
                    "media_items": [],
                    "warnings": list(warnings),
                },
            )
            payload["media_items"].append(media_item)
            payload["items"].append(media_item)
            if str(media_item.get("status") or "").strip().lower() == "ready":
                payload["status"] = "ready"
        return {"synced_at": synced_at, "by_kind": by_kind}

    @staticmethod
    def _media_owner_kind(media_kind: str) -> str:
        normalized = media_kind.strip().lower()
        if normalized in {"canvas_image", "canvas_link"}:
            return "canvas"
        if normalized in {"slides_pptx", "slides_file", "slides_import"}:
            return "slides"
        if normalized in {"document_file", "document_export"}:
            return "document"
        return ""

    def _current_artifact_sync_payload(
        self,
        kind: str,
        sync: dict[str, Any],
        *,
        document_id: str,
        document_url: str | None,
        synced_at: str,
    ) -> dict[str, Any]:
        items = [item for item in sync.get("items", []) if isinstance(item, dict)]
        media_items = [item for item in sync.get("media_items", []) if isinstance(item, dict)]
        return {
            "kind": kind,
            "status": str(sync.get("status") or "linked"),
            "document_id": document_id,
            "document_url": document_url,
            "synced_at": synced_at,
            "sync_mode": "current_artifacts_updated",
            "source_url": str(sync.get("source_url") or "").strip() or None,
            "items": items + media_items,
            "media_items": media_items,
            "warnings": [str(item) for item in sync.get("warnings", []) if str(item).strip()],
        }

    def _check_lines(self, manifest: dict[str, Any]) -> list[str]:
        lines = []
        checks = manifest.get("checks") if isinstance(manifest.get("checks"), list) else []
        for item in checks:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("key") or "检查项").strip()
            status = self._status_label(str(item.get("status") or ""))
            detail = str(item.get("detail") or "").strip()
            lines.append(f"- {label}: {status}" + (f" | {detail}" if detail else ""))
        return lines

    def _ready_deliverable_count(self, manifest: dict[str, Any]) -> int:
        deliverables = manifest.get("deliverables") if isinstance(manifest.get("deliverables"), list) else []
        return sum(1 for item in deliverables if isinstance(item, dict) and item.get("status") == "ready")

    @staticmethod
    def _section_snapshot_for_save(
        target: dict[str, Any],
        sections: list[dict[str, Any]],
        *,
        append: bool,
    ) -> list[dict[str, Any]]:
        if not append:
            return sections
        previous = target.get("section_snapshot") if isinstance(target.get("section_snapshot"), list) else []
        return [item for item in previous if isinstance(item, dict)] + sections

    @staticmethod
    def _deliverable_by_key(deliverables: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        for item in deliverables:
            if str(item.get("key") or "").strip() == key:
                return item
        return None

    def _latest_artifact_by_types(self, artifacts: Any, allowed_types: set[str]) -> dict[str, Any] | None:
        for artifact in reversed(list(artifacts or [])):
            payload = self._as_dict(artifact)
            if str(payload.get("artifact_type") or "").strip() not in allowed_types:
                continue
            if str(payload.get("status") or "").strip().lower() != "ready":
                continue
            return payload
        return None

    def _artifact_as_deliverable(self, artifact: dict[str, Any]) -> dict[str, Any]:
        payload = self._as_dict(artifact)
        artifact_type = str(payload.get("artifact_type") or "").strip()
        preview = self._preview_payload(payload.get("preview_json") or payload.get("preview"))
        if artifact_type in {"slides", "slides_package"}:
            key = "slides"
            label = "当前 PPT"
            links = self._preview_links(preview, {"pptx": "PPTX", "pdf": "PDF", "html": "HTML"})
        elif artifact_type == "canvas":
            key = "canvas"
            label = "当前 Canvas"
            links = self._preview_links(preview, {"svg": "SVG", "json": "JSON", "html": "HTML"})
        else:
            return {}
        primary_url = self._absolute_url(str(payload.get("url") or "").strip())
        if not primary_url:
            primary_url = next((link["url"] for link in links if str(link.get("label") or "").strip().upper() == "HTML"), "")
        return {
            "key": key,
            "label": label,
            "artifact_type": artifact_type,
            "title": str(payload.get("title") or label).strip() or label,
            "status": str(payload.get("status") or "ready").strip() or "ready",
            "url": primary_url,
            "links": links,
            "preview_exports": preview.get("exports") if isinstance(preview.get("exports"), dict) else {},
        }

    @staticmethod
    def _preview_payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    def _preview_links(self, preview: dict[str, Any], labels: dict[str, str]) -> list[dict[str, str]]:
        exports = preview.get("exports") if isinstance(preview.get("exports"), dict) else {}
        links: list[dict[str, str]] = []
        for key, label in labels.items():
            url = self._absolute_url(str(exports.get(key) or "").strip())
            if not url:
                continue
            links.append({"label": label, "url": url})
        return links

    @staticmethod
    def _aggregate_sync_status(statuses: list[Any]) -> str:
        normalized = [str(item or "").strip().lower() for item in statuses if str(item or "").strip()]
        if any(item == "failed" for item in normalized) and not any(item in {"ready", "partial"} for item in normalized):
            return "failed"
        if any(item in {"failed", "partial"} for item in normalized):
            return "partial"
        if any(item == "ready" for item in normalized):
            return "ready"
        if any(item == "skipped" for item in normalized):
            return "skipped"
        return "skipped"

    @staticmethod
    def _status_label(status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "ready":
            return "已生成"
        if normalized == "partial":
            return "部分可用"
        if normalized == "missing":
            return "待补齐"
        return normalized or "未知"

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            payload = dump(mode="json")
            return payload if isinstance(payload, dict) else {}
        return {}

    @staticmethod
    def _next_version(value: Any) -> int:
        try:
            return max(int(value or 1), 1) + 1
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _absolute_url(url: str) -> str:
        value = url.strip()
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        if value.startswith("/"):
            base = str(settings.artifact_public_base_url or "").strip().rstrip("/")
            return f"{base}{value}" if base else value
        return value

    @staticmethod
    def _skipped(reason: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "sync_mode": "skipped",
            "reason": reason,
            "document_id": None,
            "url": None,
            "document_url": None,
            "synced_artifact_count": 0,
            "items": [],
            "warnings": [reason],
        }
