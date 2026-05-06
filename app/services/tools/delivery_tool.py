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

        if not self._has_delivery_links(detail):
            raise ValueError("当前需求下还没有可汇总的文档、PPT 或 Canvas 链接，请先生成至少一个产物。")

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

    def _has_delivery_links(self, detail: Any) -> bool:
        return any(item.get("status") == "ready" for item in self._latest_deliverables(detail))

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
        deliverables = self._latest_deliverables(detail)
        artifacts = [
            self._artifact_item_from_deliverable(item)
            for item in deliverables
            if item.get("status") == "ready"
        ]
        ready_count = len(artifacts)
        missing_labels = [str(item["label"]) for item in deliverables if item.get("status") != "ready"]
        checks = [
            {
                "key": str(item["key"]),
                "label": str(item["label"]),
                "status": str(item["status"]),
                "detail": str(item.get("detail") or ""),
                "category": "delivery",
            }
            for item in deliverables
        ]
        summary = f"已汇总当前需求下最新产物链接：{ready_count}/3 项已生成。"
        return {
            "title": f"需求交付清单 - {getattr(detail, 'title', '') or getattr(detail, 'task_run_id', '')}",
            "task_run_id": getattr(detail, "task_run_id", ""),
            "session_id": getattr(detail, "session_id", ""),
            "requirement_id": getattr(detail, "requirement_id", None),
            "summary": summary,
            "source": {
                "source_type": getattr(detail, "source_type", ""),
                "source_ref": getattr(detail, "source_ref", None),
                "trigger_message_id": getattr(detail, "trigger_message_id", None),
                "requested_by": requested_by,
            },
            "checks": checks,
            "artifacts": artifacts,
            "artifact_summaries": deliverables,
            "deliverables": deliverables,
            "context_pack": {},
            "highlights": [
                f"已生成：{ready_count} 项；待补齐：{len(missing_labels)} 项。",
                "本清单只汇总当前需求下最新的文档、PPT 和 Canvas 链接，不重新生成内容。",
            ],
            "next_steps": [f"建议补齐：{'、'.join(missing_labels)}。"] if missing_labels else ["可将该清单作为当前需求的最终归档入口。"],
        }

    def _latest_deliverables(self, detail: Any) -> list[dict[str, Any]]:
        document = self._latest_document(detail)
        slides = self._latest_artifact_of_type(detail, {"slides", "slides_package"})
        canvas = self._latest_artifact_of_type(detail, {"canvas"})
        return [
            self._deliverable_item(
                key="document",
                label="需求文档",
                artifact_type="document",
                item=document,
                missing_detail="当前需求下还没有需求文档链接。",
            ),
            self._deliverable_item(
                key="slides",
                label="答辩 PPT",
                artifact_type="slides_package",
                item=slides,
                missing_detail="当前需求下还没有 PPT 链接。",
            ),
            self._deliverable_item(
                key="canvas",
                label="Canvas / 流程图",
                artifact_type="canvas",
                item=canvas,
                missing_detail="当前需求下还没有 Canvas 链接。",
            ),
        ]

    def _latest_document(self, detail: Any) -> Any | None:
        documents = list(getattr(detail, "session_documents", []) or [])
        if documents:
            current = [item for item in documents if bool(_field(item, "is_current"))]
            candidates = current or documents
            return max(candidates, key=_sort_key)
        return self._latest_artifact_of_type(detail, {"document", "doc", "feishu_doc"})

    def _latest_artifact_of_type(self, detail: Any, artifact_types: set[str]) -> Any | None:
        candidates = [
            item
            for item in (getattr(detail, "artifacts", []) or [])
            if str(_field(item, "artifact_type") or "").strip() in artifact_types
        ]
        if not candidates:
            return None
        return max(candidates, key=_sort_key)

    def _deliverable_item(
        self,
        *,
        key: str,
        label: str,
        artifact_type: str,
        item: Any | None,
        missing_detail: str,
    ) -> dict[str, Any]:
        if item is None:
            return {
                "key": key,
                "label": label,
                "artifact_type": artifact_type,
                "title": label,
                "status": "missing",
                "detail": missing_detail,
                "url": None,
                "links": [],
            }
        title = str(_field(item, "title") or _field(item, "document_id") or label).strip() or label
        url = str(_field(item, "url") or "").strip() or None
        links = self._links_for_item(item, primary_url=url)
        status = "ready" if links or url else "partial"
        detail = f"最新{label}：{title}" if status == "ready" else f"已记录{label}，但缺少可打开链接。"
        return {
            "key": key,
            "label": label,
            "artifact_id": _field(item, "artifact_id") or _field(item, "document_id"),
            "artifact_type": str(_field(item, "artifact_type") or artifact_type),
            "title": title,
            "status": status,
            "provider": _field(item, "provider") or ("feishu" if key == "document" else "local"),
            "url": url,
            "version": _field(item, "version") or 1,
            "updated_at": str(_field(item, "updated_at") or _field(item, "created_at") or ""),
            "detail": detail,
            "links": links,
        }

    def _links_for_item(self, item: Any, *, primary_url: str | None) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        if primary_url:
            links.append({"label": "打开", "url": primary_url})
        exports = _preview(item).get("exports")
        if isinstance(exports, dict):
            for key, value in exports.items():
                url = str(value or "").strip()
                if url and all(existing["url"] != url for existing in links):
                    links.append({"label": str(key).upper(), "url": url})
        return links

    def _artifact_item_from_deliverable(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": item.get("artifact_id"),
            "artifact_type": item.get("artifact_type"),
            "title": item.get("title") or item.get("label") or "协作产物",
            "status": item.get("status") or "ready",
            "provider": item.get("provider") or "local",
            "url": item.get("url"),
            "version": item.get("version") or 1,
            "links": item.get("links") if isinstance(item.get("links"), list) else [],
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


def _sort_key(item: object) -> tuple[str, int]:
    updated = _field(item, "updated_at") or _field(item, "created_at") or ""
    version = _positive_int(_field(item, "version"))
    return (str(updated), version)
