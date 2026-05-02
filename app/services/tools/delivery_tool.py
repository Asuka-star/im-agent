from __future__ import annotations

import json
from typing import Any

from app.core.config import settings
from app.services.delivery_artifact_service import DeliveryArtifactService
from app.services.feishu_card_builder import FeishuArtifactCardBuilder
from app.services.task_artifact_verifier import TaskArtifactVerifier
from app.services.task_context_pack import TaskContextPackBuilder
from app.services.task_run_service import TaskRunService
from app.utils.values import coerce_positive_int


class DeliveryTool:
    """Builds and persists task delivery bundles for final handoff."""

    def __init__(
        self,
        *,
        delivery_artifact_service: DeliveryArtifactService,
        task_run_service: TaskRunService,
        artifact_verifier: TaskArtifactVerifier | None = None,
        context_pack_builder: TaskContextPackBuilder | None = None,
        message_api: Any | None = None,
        card_builder: FeishuArtifactCardBuilder | None = None,
    ) -> None:
        self.delivery_artifact_service = delivery_artifact_service
        self.task_run_service = task_run_service
        self.artifact_verifier = artifact_verifier or TaskArtifactVerifier()
        self.context_pack_builder = context_pack_builder or TaskContextPackBuilder()
        self.message_api = message_api
        self.card_builder = card_builder or FeishuArtifactCardBuilder()

    def bundle_from_task_run(self, task_run_id: str, *, requested_by: str = "pilot_workbench"):
        detail = self.task_run_service.get_task_run(task_run_id)
        if detail is None:
            return None

        manifest = self.build_manifest(detail, requested_by=requested_by)
        artifact = self.delivery_artifact_service.persist_bundle(
            manifest,
            task_run_id=task_run_id,
            session_id=detail.session_id,
        )
        delivery_card = self._send_delivery_card(detail, artifact)
        self.task_run_service.upsert_step(
            task_run_id,
            step_key="delivery_bundle",
            title="生成交付包",
            step_type="artifact",
            status="done",
            input_payload={"requested_by": requested_by},
            output_payload={
                "url": artifact.get("url"),
                "artifact_count": len(artifact.get("preview", {}).get("artifacts", [])),
                "ready_checks": self._count_checks(artifact.get("preview", {}), "ready"),
                "im_card_sent": delivery_card["sent"],
                "im_card_error": delivery_card["error"],
            },
        )
        self.task_run_service.create_artifact(
            task_run_id,
            artifact_type=str(artifact.get("artifact_type") or "delivery_bundle"),
            title=str(artifact.get("title") or "任务交付包"),
            provider=str(artifact.get("provider") or "local"),
            status=str(artifact.get("status") or "ready"),
            url=str(artifact.get("url") or "").strip() or None,
            preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
            version=coerce_positive_int(artifact.get("version")),
        )
        update_kwargs = {"latest_summary": str(manifest.get("summary") or "交付包已生成")}
        if getattr(detail, "status", "") == "completed":
            update_kwargs["stage"] = "delivered"
            update_kwargs["status"] = "completed"
        self.task_run_service.update_task_run(task_run_id, **update_kwargs)
        return self.task_run_service.get_task_run(task_run_id)

    def _send_delivery_card(self, detail: Any, artifact: dict) -> dict[str, Any]:
        if not settings.feishu_reply_enabled or not settings.feishu_reply_card_enabled:
            return {"sent": False, "error": None}
        if self.message_api is None:
            return {"sent": False, "error": "message api unavailable"}
        chat_id = str(getattr(detail, "source_ref", "") or "").strip()
        if not chat_id or str(getattr(detail, "source_type", "") or "") == "workbench":
            return {"sent": False, "error": None}
        card = self.card_builder.build_artifact_card(
            title="任务交付包已生成",
            mode="delivery",
            artifacts=[artifact],
            summary=str(artifact.get("preview", {}).get("summary") or ""),
        )
        if card is None:
            return {"sent": False, "error": None}
        try:
            self.message_api.send_interactive_message(chat_id, card, receive_id_type="chat_id")
        except Exception as exc:  # noqa: BLE001
            return {"sent": False, "error": str(exc)}
        return {"sent": True, "error": None}

    def build_manifest(self, detail, *, requested_by: str) -> dict:
        raw_artifacts = [
            item
            for item in (getattr(detail, "artifacts", []) or [])
            if str(_field(item, "artifact_type") or "") != "delivery_bundle"
        ]
        artifacts = [self._artifact_item(item) for item in raw_artifacts]
        documents = getattr(detail, "session_documents", []) or []
        appended_documents: list[dict] = []
        if documents and not any(item.get("artifact_type") == "document" for item in artifacts):
            for document in documents:
                appended_documents.append(
                    {
                        "artifact_id": _field(document, "document_id"),
                        "artifact_type": "document",
                        "title": _field(document, "title") or "协作文档",
                        "status": "ready",
                        "provider": "feishu" if _field(document, "url") else "local",
                        "url": _field(document, "url"),
                        "version": _field(document, "version") or 1,
                    }
                )
            artifacts.extend(appended_documents)
        checks = self.artifact_verifier.build_for_task_run(detail, assume_delivery_ready=True)
        ready_count = sum(1 for item in checks if item.get("status") == "ready")
        partial_count = sum(1 for item in checks if item.get("status") == "partial")
        missing_count = sum(1 for item in checks if item.get("status") == "missing")
        artifact_summaries = [
            self._artifact_summary(item)
            for item in [*raw_artifacts, *appended_documents]
        ]
        context_pack = _as_dict(getattr(detail, "context_pack", None)) or self.context_pack_builder.build_for_task_run(detail)
        summary = (
            f"本次任务已整理 {len(artifacts)} 个交付物，"
            f"{ready_count}/{len(checks)} 个验收项满足。"
        )
        return {
            "title": f"任务交付包 · {getattr(detail, 'title', '') or getattr(detail, 'task_run_id', '')}",
            "task_run_id": getattr(detail, "task_run_id", ""),
            "session_id": getattr(detail, "session_id", ""),
            "summary": summary,
            "source": {
                "source_type": getattr(detail, "source_type", ""),
                "source_ref": getattr(detail, "source_ref", None),
                "trigger_message_id": getattr(detail, "trigger_message_id", None),
                "requested_by": requested_by,
            },
            "checks": checks,
            "artifacts": artifacts,
            "artifact_summaries": artifact_summaries,
            "context_pack": context_pack,
            "highlights": [
                f"验收清单：{ready_count} 项已满足，{partial_count} 项部分满足，{missing_count} 项待补齐。",
                f"交付物：{len(artifacts)} 个，可打开链接 {sum(1 for item in artifacts if str(item.get('url') or '').strip())} 个。",
                self._scene_highlight(checks),
            ],
            "next_steps": self._next_steps(checks, artifacts),
        }

    def _artifact_item(self, artifact) -> dict:
        return {
            "artifact_id": _field(artifact, "artifact_id"),
            "artifact_type": _field(artifact, "artifact_type"),
            "title": _field(artifact, "title") or "协作产物",
            "status": _field(artifact, "status") or "ready",
            "provider": _field(artifact, "provider") or "local",
            "url": _field(artifact, "url"),
            "version": _field(artifact, "version") or 1,
        }

    def _artifact_summary(self, artifact) -> dict:
        artifact_type = str(_field(artifact, "artifact_type") or "artifact")
        preview = _preview(artifact)
        summary = {
            "artifact_id": _field(artifact, "artifact_id"),
            "artifact_type": artifact_type,
            "label": self._artifact_label(artifact_type),
            "title": _field(artifact, "title") or self._artifact_label(artifact_type),
            "status": _field(artifact, "status") or "ready",
            "url": _field(artifact, "url"),
            "version": _field(artifact, "version") or 1,
            "metrics": [],
            "highlights": [],
            "warnings": [],
            "exports": self._exports_from_preview(preview),
        }
        if artifact_type == "slides_package":
            self._fill_slides_summary(summary, preview)
        elif artifact_type == "canvas":
            self._fill_canvas_summary(summary, preview)
        elif artifact_type in {"document", "doc", "feishu_doc"}:
            self._fill_document_summary(summary, preview)
        else:
            summary["highlights"].append("已纳入交付包归档。")
        return summary

    def _fill_slides_summary(self, summary: dict, preview: dict[str, Any]) -> None:
        slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
        notes = [
            item
            for item in slides
            if isinstance(item, dict) and str(item.get("speaker_notes") or "").strip()
        ]
        total_duration = sum(_positive_int(item.get("duration_sec")) for item in slides if isinstance(item, dict))
        dense_pages = [
            index + 1
            for index, item in enumerate(slides)
            if isinstance(item, dict) and len(item.get("bullets") if isinstance(item.get("bullets"), list) else []) > 5
        ]
        missing_notes = [
            index + 1
            for index, item in enumerate(slides)
            if isinstance(item, dict) and not str(item.get("speaker_notes") or "").strip()
        ]
        summary["metrics"].extend(
            [
                f"{len(slides)} 页",
                f"讲者备注 {len(notes)}/{len(slides)} 页",
            ]
        )
        if total_duration:
            summary["metrics"].append(f"建议讲述 {round(total_duration / 60, 1)} 分钟")
        export_labels = {str(item.get("label") or "").lower() for item in summary.get("exports", []) if isinstance(item, dict)}
        if {"html", "pptx"} <= export_labels:
            summary["highlights"].append("已提供 HTML 预览和 PPTX 导出。")
        elif export_labels:
            summary["highlights"].append("已提供导出：" + "、".join(sorted(label.upper() for label in export_labels)))
        else:
            summary["warnings"].append("缺少 HTML / PPTX 导出链接。")
        if "pdf" in export_labels:
            summary["highlights"].append("已提供 PDF 交付稿。")
        else:
            summary["warnings"].append("缺少 PDF 交付稿。")
        if notes:
            summary["highlights"].append("已生成可用于排练的讲者备注。")
        if missing_notes:
            summary["warnings"].append("缺少讲者备注：P" + "、P".join(str(item) for item in missing_notes))
        if dense_pages:
            summary["warnings"].append("页面内容偏密：P" + "、P".join(str(item) for item in dense_pages))

    def _fill_canvas_summary(self, summary: dict, preview: dict[str, Any]) -> None:
        canvas_summary = preview.get("summary") if isinstance(preview.get("summary"), dict) else {}
        shapes = preview.get("shapes") if isinstance(preview.get("shapes"), list) else []
        node_count = _positive_int(canvas_summary.get("node_count")) or sum(
            1 for item in shapes if isinstance(item, dict) and str(item.get("type") or "") != "arrow"
        )
        arrow_count = _positive_int(canvas_summary.get("arrow_count")) or sum(
            1 for item in shapes if isinstance(item, dict) and str(item.get("type") or "") == "arrow"
        )
        template = str(preview.get("template") or canvas_summary.get("template") or "flow")
        groups = canvas_summary.get("groups") if isinstance(canvas_summary.get("groups"), list) else []
        summary["metrics"].extend([self._canvas_template_label(template), f"{node_count} 节点", f"{arrow_count} 连线"])
        if groups:
            summary["highlights"].append("覆盖分组：" + "、".join(str(item) for item in groups[:6]))
        export_labels = {str(item.get("label") or "").lower() for item in summary.get("exports", []) if isinstance(item, dict)}
        if {"json", "svg", "html"} <= export_labels:
            summary["highlights"].append("已提供 JSON / SVG / HTML 三种导出。")
        elif export_labels:
            summary["highlights"].append("已提供导出：" + "、".join(sorted(label.upper() for label in export_labels)))
        else:
            summary["warnings"].append("缺少 Canvas 导出链接。")

    def _fill_document_summary(self, summary: dict, preview: dict[str, Any]) -> None:
        sections = preview.get("sections") if isinstance(preview.get("sections"), list) else []
        if sections:
            summary["metrics"].append(f"{len(sections)} 个章节")
            headings = [
                str(item.get("heading") or "").strip()
                for item in sections
                if isinstance(item, dict) and str(item.get("heading") or "").strip()
            ]
            if headings:
                summary["highlights"].append("核心章节：" + "、".join(headings[:4]))
        if str(summary.get("url") or "").strip():
            summary["highlights"].append("已提供可打开的协作文档链接。")
        else:
            summary["warnings"].append("缺少可打开链接，建议同步到飞书文档。")

    def _exports_from_preview(self, preview: dict[str, Any]) -> list[dict[str, str]]:
        exports = preview.get("exports") if isinstance(preview.get("exports"), dict) else {}
        result = []
        for key, value in exports.items():
            url = str(value or "").strip()
            if url:
                result.append({"label": str(key).upper(), "url": url})
        return result

    def _scene_highlight(self, checks: list[dict]) -> str:
        scene_checks = [item for item in checks if str(item.get("category") or "").startswith("scene")]
        ready = sum(1 for item in scene_checks if item.get("status") == "ready")
        return f"场景 C/D：{ready}/{len(scene_checks)} 个关键产物检查已满足。"

    def _artifact_label(self, artifact_type: str) -> str:
        labels = {
            "document": "协作文档",
            "doc": "协作文档",
            "feishu_doc": "飞书文档",
            "slides_package": "演示稿",
            "canvas": "白板 / Canvas",
            "delivery_bundle": "交付包",
        }
        return labels.get(artifact_type, "协作产物")

    def _canvas_template_label(self, template: str) -> str:
        labels = {
            "flow": "流程图",
            "risk": "风险应对图",
            "module": "模块分工图",
        }
        return labels.get(template, template or "流程图")

    def _next_steps(self, checks: list[dict], artifacts: list[dict]) -> list[str]:
        next_steps = ["把交付包链接贴回 IM 会话，作为本轮讨论的归档入口。"]
        missing = [str(item.get("label")) for item in checks if item.get("status") == "missing"]
        if missing:
            next_steps.append("补齐：" + "、".join(missing))
        if any(item.get("artifact_type") == "slides_package" for item in artifacts):
            next_steps.append("进入排练模式检查讲者备注，并按评委视角做最后一轮修订。")
        return next_steps

    def _count_checks(self, manifest: dict, status: str) -> int:
        checks = manifest.get("checks") if isinstance(manifest, dict) else []
        if not isinstance(checks, list):
            return 0
        return sum(1 for item in checks if isinstance(item, dict) and item.get("status") == status)


def _field(item: object, field: str):
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _preview(item: object) -> dict[str, Any]:
    raw = _field(item, "preview") or _field(item, "preview_json")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _positive_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump(mode="json")
        return data if isinstance(data, dict) else {}
    return {}
