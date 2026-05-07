from __future__ import annotations

import json
from typing import Any


def normalize_section_paragraphs(paragraphs: list[Any]) -> list[Any]:
    normalized: list[Any] = []
    seen: set[str] = set()
    for item in paragraphs if isinstance(paragraphs, list) else []:
        cleaned = _normalize_paragraph_item(item)
        if cleaned is None:
            continue
        signature = paragraph_signature(cleaned)
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append(cleaned)
    return normalized


def build_section_snapshot(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        paragraphs = normalize_section_paragraphs(
            section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else [],
        )
        if not heading and not paragraphs:
            continue
        snapshot.append({"heading": heading, "paragraphs": paragraphs})
    return snapshot


def section_snapshot_map(snapshot: list[dict[str, Any]]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for section in snapshot:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        paragraphs = normalize_section_paragraphs(
            section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else [],
        )
        result[heading] = paragraphs
    return result


def paragraph_signature(item: Any) -> str:
    if isinstance(item, str):
        return f"text:{item.strip()}"
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def paragraph_preview_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return str(item).strip()
    paragraph_type = str(item.get("type") or "").strip().lower()
    if paragraph_type == "image":
        caption = str(item.get("caption") or "").strip()
        token = str(item.get("token") or item.get("source") or "").strip()
        summary = caption or token or "图片"
        return f"[图片] {summary}".strip()
    if paragraph_type == "table":
        rows = item.get("rows") if isinstance(item.get("rows"), list) else []
        row_count = len(rows)
        column_count = max((len(row) for row in rows if isinstance(row, list)), default=0)
        return f"[表格] {row_count}x{column_count}"
    if paragraph_type in {"callout", "note"}:
        text = str(item.get("text") or "").strip()
        return f"[提示] {text}".strip()
    if paragraph_type == "divider":
        return "[分割线]"
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _normalize_paragraph_item(item: Any) -> Any | None:
    if isinstance(item, str):
        text = item.strip()
        return text or None
    if isinstance(item, dict):
        normalized = _normalize_json_like(item)
        return normalized if isinstance(normalized, dict) and normalized else None
    return None


def _normalize_json_like(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            normalized_item = _normalize_json_like(item)
            if normalized_item is None:
                continue
            normalized[normalized_key] = normalized_item
        return normalized
    if isinstance(value, list):
        result = []
        for item in value:
            normalized_item = _normalize_json_like(item)
            if normalized_item is None:
                continue
            result.append(normalized_item)
        return result
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value).strip()
    return text or None
