from __future__ import annotations

import json
from typing import Any


class TaskContextPackBuilder:
    """Builds a visible summary of the materials used by an Agent task run."""

    def build_for_task_run(self, detail: Any) -> dict[str, Any]:
        used_sources = self._used_sources(detail)
        missing_items = self._missing_items(detail)
        suggested_inputs = self._suggested_inputs(detail, missing_items)
        return {
            "summary": (
                f"已汇总 {len(used_sources)} 项可追溯材料，"
                f"{len(missing_items)} 项信息建议补充。"
            ),
            "used_sources": used_sources,
            "missing_items": missing_items,
            "suggested_inputs": suggested_inputs,
        }

    def _used_sources(self, detail: Any) -> list[dict[str, str | None]]:
        sources: list[dict[str, str | None]] = []
        source_type = str(getattr(detail, "source_type", "") or "").strip()
        source_ref = str(getattr(detail, "trigger_message_id", None) or getattr(detail, "source_ref", None) or "").strip()
        if source_type:
            sources.append(
                _item(
                    "im",
                    "飞书 IM 会话",
                    source_ref or f"来源类型：{source_type}",
                    status="ready" if source_ref else "partial",
                )
            )

        steps = list(getattr(detail, "steps", []) or [])
        if steps:
            done = sum(1 for step in steps if str(_field(step, "status") or "") in {"done", "completed"})
            sources.append(_item("plan", "Agent 编排记录", f"{len(steps)} 个步骤，{done} 个已完成"))

        seen_document_signatures: set[tuple[str, str]] = set()
        for document in list(getattr(detail, "session_documents", []) or [])[:4]:
            title = str(_field(document, "title") or "协作文档")
            version = str(_field(document, "version") or 1)
            sync_mode = str(_field(document, "sync_mode") or "synced")
            current = "当前文档，" if bool(_field(document, "is_current")) else ""
            url = str(_field(document, "url") or "").strip() or None
            seen_document_signatures.add(_document_signature(_document_payload(document)))
            sources.append(
                _item(
                    "document",
                    title,
                    f"{current}v{version}，{sync_mode}",
                    status="ready" if url else "partial",
                    url=url,
                )
            )

        artifact_sources = [
            artifact
            for artifact in (getattr(detail, "artifacts", []) or [])
            if str(_field(artifact, "artifact_type") or "") != "delivery_bundle"
        ]
        for artifact in artifact_sources[:5]:
            artifact_type = str(_field(artifact, "artifact_type") or "artifact")
            if artifact_type in {"document", "doc", "feishu_doc"}:
                signature = _document_signature(_document_payload(artifact))
                if signature in seen_document_signatures:
                    continue
                seen_document_signatures.add(signature)
            sources.append(
                _item(
                    artifact_type,
                    str(_field(artifact, "title") or _artifact_label(artifact_type)),
                    f"{_artifact_label(artifact_type)}，{str(_field(artifact, 'provider') or 'local')}，v{str(_field(artifact, 'version') or 1)}",
                    status=str(_field(artifact, "status") or "ready"),
                    url=str(_field(artifact, "url") or "").strip() or None,
                )
            )
        return sources

    def _missing_items(self, detail: Any) -> list[dict[str, str | None]]:
        artifacts = list(getattr(detail, "artifacts", []) or [])
        artifact_types = {str(_field(item, "artifact_type") or "") for item in artifacts}
        documents = list(getattr(detail, "session_documents", []) or [])
        missing: list[dict[str, str | None]] = []

        has_document = bool(documents) or bool({"document", "doc", "feishu_doc"} & artifact_types)
        if not has_document:
            missing.append(
                _item(
                    "document",
                    "协作文档依据",
                    "当前任务还没有可追溯的文档产物，建议先生成或同步飞书 Docx。",
                    status="missing",
                )
            )
        elif documents and not any(str(_field(item, "url") or "").strip() for item in documents):
            missing.append(
                _item(
                    "document_link",
                    "飞书文档链接",
                    "已有文档记录，但缺少可打开链接。",
                    status="partial",
                )
            )

        if "canvas" not in artifact_types:
            missing.append(
                _item(
                    "canvas",
                    "白板 / Canvas",
                    "尚未生成流程、模块或风险白板，场景 C 展示会不够完整。",
                    status="missing",
                )
            )
        if not {"slides_package", "slides"} & artifact_types:
            missing.append(
                _item(
                    "slides",
                    "演示稿",
                    "尚未生成汇报 PPT，场景 D 展示会不够完整。",
                    status="missing",
                )
            )
        if not str(getattr(detail, "trigger_message_id", None) or getattr(detail, "source_ref", None) or "").strip():
            missing.append(
                _item(
                    "im_trace",
                    "原始 IM 定位",
                    "缺少原始消息 ID 或来源引用，回溯会话依据时说服力较弱。",
                    status="partial",
                )
            )
        pending = [
            item
            for item in (getattr(detail, "confirmations", []) or [])
            if str(_field(item, "status") or "") not in {"answered", "resolved"}
        ]
        if pending:
            missing.append(
                _item(
                    "confirmation",
                    "用户确认",
                    f"仍有 {len(pending)} 个确认节点未处理。",
                    status="partial",
                )
            )
        return missing

    def _suggested_inputs(self, detail: Any, missing_items: list[dict[str, str | None]]) -> list[str]:
        missing_kinds = {str(item.get("kind") or "") for item in missing_items}
        suggestions: list[str] = []
        if "document" in missing_kinds:
            suggestions.append("补充目标受众、业务背景、约束条件，再生成协作文档。")
        if "canvas" in missing_kinds:
            suggestions.append("补充流程阶段、关键依赖、风险和缓解措施，用于生成白板。")
        if "slides" in missing_kinds:
            suggestions.append("补充汇报页数、听众角色、演示时长和希望强调的价值点。")
        if "confirmation" in missing_kinds:
            suggestions.append("先回答当前确认问题，再继续生成或修订产物。")
        if not suggestions:
            suggestions.append("当前上下文较完整，可以进入排练、修订或交付归档。")
        return suggestions[:4]


def _item(kind: str, label: str, detail: str, *, status: str = "ready", url: str | None = None) -> dict[str, str | None]:
    return {
        "kind": kind,
        "label": label,
        "detail": detail,
        "status": status,
        "url": url,
    }


def _field(item: object, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _preview(item: object) -> dict[str, Any]:
    raw = _field(item, "preview")
    if raw is None:
        raw = _field(item, "preview_json")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _document_payload(item: object) -> dict[str, Any]:
    payload = {
        "document_id": _field(item, "document_id"),
        "url": _field(item, "url"),
        "title": _field(item, "title"),
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


def _artifact_label(artifact_type: str) -> str:
    labels = {
        "document": "协作文档",
        "doc": "协作文档",
        "feishu_doc": "飞书文档",
        "slides": "演示稿",
        "slides_package": "演示稿",
        "canvas": "白板 / Canvas",
    }
    return labels.get(artifact_type, "协作产物")
