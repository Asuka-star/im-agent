from __future__ import annotations

import json
from typing import Any


class TaskArtifactVerifier:
    """Builds judge-visible checks from task-run state and generated artifacts."""

    def build_for_task_run(self, detail: Any, *, assume_delivery_ready: bool = False) -> list[dict[str, str]]:
        artifacts = [
            item
            for item in (getattr(detail, "artifacts", []) or [])
            if str(_field(item, "artifact_type") or "") != "delivery_bundle"
        ]
        all_artifacts = list(getattr(detail, "artifacts", []) or [])
        checks = [
            self._im_entry_check(detail),
            self._agent_plan_check(detail),
            self._document_check(detail, artifacts),
            self._presentation_or_canvas_check(artifacts),
            self._canvas_check(artifacts),
            self._slides_check(artifacts),
            self._rehearsal_check(artifacts),
            self._confirmation_check(detail),
            self._shareable_links_check(detail, artifacts),
            self._delivery_check(detail, all_artifacts, artifacts, assume_delivery_ready=assume_delivery_ready),
        ]
        return checks

    def _im_entry_check(self, detail: Any) -> dict[str, str]:
        source_type = str(getattr(detail, "source_type", "") or "").strip()
        source_ref = str(getattr(detail, "trigger_message_id", None) or getattr(detail, "source_ref", None) or "").strip()
        return _check(
            "im_entry",
            "IM 入口",
            "ready" if source_type else "missing",
            source_ref or "工作台触发",
            category="workflow",
        )

    def _agent_plan_check(self, detail: Any) -> dict[str, str]:
        steps = list(getattr(detail, "steps", []) or [])
        done = sum(1 for item in steps if str(_field(item, "status") or "") in {"done", "completed"})
        return _check(
            "agent_plan",
            "Agent 编排轨迹",
            "ready" if steps else "missing",
            f"记录 {len(steps)} 个执行步骤，{done} 个已完成",
            category="workflow",
        )

    def _document_check(self, detail: Any, artifacts: list[Any]) -> dict[str, str]:
        documents = list(getattr(detail, "session_documents", []) or [])
        document_artifacts = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") in {"document", "doc", "feishu_doc"}
        ]
        unique_documents = _merge_document_sources(documents, document_artifacts)
        link_count = sum(1 for item in unique_documents if str(item.get("url") or "").strip())
        total = len(unique_documents)
        if total == 0:
            status = "missing"
            detail_text = "尚未生成文档产物"
        elif link_count > 0:
            status = "ready"
            detail_text = f"已记录 {total} 个文档产物，{link_count} 个可打开链接"
        else:
            status = "partial"
            detail_text = f"已记录 {total} 个文档产物，但缺少可打开链接"
        return _check("document", "文档产物", status, detail_text, category="scene_c")

    def _presentation_or_canvas_check(self, artifacts: list[Any]) -> dict[str, str]:
        artifact_types = {str(_field(item, "artifact_type") or "") for item in artifacts}
        has_visual = bool({"slides_package", "canvas"} & artifact_types)
        return _check(
            "presentation_or_canvas",
            "演示稿/画布",
            "ready" if has_visual else "missing",
            "已包含演示稿或自由画布" if has_visual else "尚未生成演示稿或画布",
            category="scene_cd",
        )

    def _canvas_check(self, artifacts: list[Any]) -> dict[str, str]:
        canvas_items = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") == "canvas"
        ]
        if not canvas_items:
            return _check("canvas", "白板 / Canvas", "missing", "尚未生成白板产物", category="scene_c")
        latest = canvas_items[-1]
        preview = _preview(latest)
        exports = _exports(preview)
        shapes = preview.get("shapes") if isinstance(preview.get("shapes"), list) else []
        has_schema = str(preview.get("schema") or "").strip() == "im-agent.canvas.v1"
        missing_exports = [key for key in ("json", "svg", "html") if not str(exports.get(key) or "").strip()]
        status = "ready" if has_schema and shapes and not missing_exports else "partial"
        detail = f"{len(shapes)} 个形状"
        if missing_exports:
            detail += "，缺少导出：" + "、".join(missing_exports)
        else:
            detail += "，JSON / SVG / HTML 已生成"
        return _check("canvas", "白板 / Canvas", status, detail, category="scene_c")

    def _slides_check(self, artifacts: list[Any]) -> dict[str, str]:
        slides_items = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") == "slides_package"
        ]
        if not slides_items:
            return _check("slides", "演示稿", "missing", "尚未生成演示稿产物", category="scene_d")
        latest = slides_items[-1]
        preview = _preview(latest)
        slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
        exports = _exports(preview)
        missing_exports = [key for key in ("html", "pptx") if not str(exports.get(key) or "").strip()]
        has_pdf = bool(str(exports.get("pdf") or "").strip())
        titled = sum(1 for item in slides if isinstance(item, dict) and str(item.get("title") or "").strip())
        status = "ready" if slides and titled == len(slides) and not missing_exports else "partial"
        detail = f"{len(slides)} 页，{titled} 页有标题"
        if missing_exports:
            detail += "，缺少导出：" + "、".join(missing_exports)
        else:
            detail += "，HTML / PPTX 已生成"
        detail += "，PDF 已生成" if has_pdf else "，PDF 待补"
        return _check("slides", "演示稿", status, detail, category="scene_d")

    def _rehearsal_check(self, artifacts: list[Any]) -> dict[str, str]:
        slides_items = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") == "slides_package"
        ]
        if not slides_items:
            return _check("rehearsal", "排练辅助", "missing", "需要先生成演示稿", category="scene_d")
        preview = _preview(slides_items[-1])
        slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
        notes = [
            item
            for item in slides
            if isinstance(item, dict) and str(item.get("speaker_notes") or "").strip()
        ]
        durations = [
            item
            for item in slides
            if isinstance(item, dict) and _positive_int(item.get("duration_sec")) > 0
        ]
        if slides and len(notes) == len(slides):
            status = "ready"
        elif notes:
            status = "partial"
        else:
            status = "missing"
        detail = f"{len(notes)}/{len(slides)} 页有讲者备注，{len(durations)} 页有建议时长"
        return _check("rehearsal", "排练辅助", status, detail, category="scene_d")

    def _confirmation_check(self, detail: Any) -> dict[str, str]:
        pending = [
            item
            for item in (getattr(detail, "confirmations", []) or [])
            if str(_field(item, "status") or "") not in {"answered", "resolved"}
        ]
        return _check(
            "confirmation",
            "人工确认",
            "ready" if not pending else "partial",
            "没有待处理确认" if not pending else f"仍有 {len(pending)} 个待确认节点",
            category="workflow",
        )

    def _shareable_links_check(self, detail: Any, artifacts: list[Any]) -> dict[str, str]:
        documents = list(getattr(detail, "session_documents", []) or [])
        document_artifacts = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") in {"document", "doc", "feishu_doc"}
        ]
        other_artifacts = [
            item
            for item in artifacts
            if str(_field(item, "artifact_type") or "") not in {"document", "doc", "feishu_doc"}
        ]
        unique_documents = _merge_document_sources(documents, document_artifacts)
        link_count = sum(1 for item in other_artifacts if str(_field(item, "url") or "").strip())
        link_count += sum(1 for item in unique_documents if str(item.get("url") or "").strip())
        deliverable_count = len(other_artifacts) + len(unique_documents)
        status = "ready" if link_count else "partial" if deliverable_count else "missing"
        detail = f"{link_count} 个产物可直接打开" if link_count else "当前只有结构化记录，缺少可打开链接"
        return _check("shareable_links", "可分享链接", status, detail, category="delivery")

    def _delivery_check(
        self,
        detail: Any,
        all_artifacts: list[Any],
        deliverable_artifacts: list[Any],
        *,
        assume_delivery_ready: bool,
    ) -> dict[str, str]:
        has_bundle = assume_delivery_ready or any(
            str(_field(item, "artifact_type") or "") == "delivery_bundle"
            for item in all_artifacts
        )
        if has_bundle:
            return _check("delivery_bundle", "交付包", "ready", "交付包已生成", category="delivery")
        document_artifacts = [
            item
            for item in deliverable_artifacts
            if str(_field(item, "artifact_type") or "") in {"document", "doc", "feishu_doc"}
        ]
        other_artifacts = [
            item
            for item in deliverable_artifacts
            if str(_field(item, "artifact_type") or "") not in {"document", "doc", "feishu_doc"}
        ]
        unique_documents = _merge_document_sources(
            list(getattr(detail, "session_documents", []) or []),
            document_artifacts,
        )
        deliverable_count = len(other_artifacts) + len(unique_documents)
        if deliverable_count:
            return _check(
                "delivery_bundle",
                "交付包",
                "partial",
                f"已有 {deliverable_count} 个可打包产物",
                category="delivery",
            )
        return _check("delivery_bundle", "交付包", "missing", "尚无可打包产物", category="delivery")


def _check(key: str, label: str, status: str, detail: str, *, category: str) -> dict[str, str]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "category": category,
    }


def _field(item: object, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _preview(item: object) -> dict[str, Any]:
    if isinstance(item, dict):
        raw = item.get("preview") or item.get("preview_json")
    else:
        raw = getattr(item, "preview", None) or getattr(item, "preview_json", None)
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _exports(preview: dict[str, Any]) -> dict[str, Any]:
    exports = preview.get("exports")
    return exports if isinstance(exports, dict) else {}


def _positive_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _merge_document_sources(documents: list[Any], document_artifacts: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in documents:
        payload = _document_payload(item)
        signature = _document_signature(payload)
        if signature in seen:
            continue
        seen.add(signature)
        merged.append(payload)
    for item in document_artifacts:
        payload = _document_payload(item)
        signature = _document_signature(payload)
        if signature in seen:
            continue
        seen.add(signature)
        merged.append(payload)
    return merged


def _document_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        payload = dict(item)
    else:
        payload = {
            "document_id": getattr(item, "document_id", None),
            "url": getattr(item, "url", None),
            "title": getattr(item, "title", None),
        }
        preview = _preview(item)
        sync = preview.get("sync") if isinstance(preview.get("sync"), dict) else {}
        if not payload.get("document_id"):
            payload["document_id"] = sync.get("document_id")
        if not payload.get("url"):
            payload["url"] = sync.get("url") or preview.get("url")
        if not payload.get("title"):
            payload["title"] = sync.get("title") or preview.get("title")
    return payload


def _document_signature(payload: dict[str, Any]) -> tuple[str, str]:
    document_id = str(payload.get("document_id") or "").strip()
    if document_id:
        return ("document_id", document_id)
    url = str(payload.get("url") or "").strip()
    if url:
        return ("url", url)
    title = str(payload.get("title") or "").strip().lower()
    if title:
        return ("title", title)
    return ("fallback", json.dumps(payload, ensure_ascii=False, sort_keys=True))
