from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.services.app_state import AppStateService
from app.utils.values import coerce_positive_int


class SessionDocumentService:
    """Persists the current collaborative document for a session."""

    KEY_PREFIX = "session_doc"
    LIST_KEY_PREFIX = "session_docs"

    def __init__(self, state_service: AppStateService | None = None) -> None:
        self.state_service = state_service or AppStateService()

    def get_current_document(self, session_id: str) -> dict[str, Any] | None:
        raw = self.state_service.get_value(self._key(session_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def list_documents(self, session_id: str) -> list[dict[str, Any]]:
        raw = self.state_service.get_value(self._list_key(session_id))
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        documents = [item for item in payload if isinstance(item, dict)]
        current = self.get_current_document(session_id)
        current_id = str(current.get("document_id") or "").strip() if isinstance(current, dict) else ""
        result: list[dict[str, Any]] = []
        for document in documents:
            item = dict(document)
            item["is_current"] = bool(current_id and item.get("document_id") == current_id)
            result.append(item)
        result.sort(
            key=lambda item: (
                bool(item.get("is_current")),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return result

    def get_document(self, session_id: str, document_id: str) -> dict[str, Any] | None:
        target = document_id.strip()
        if not target:
            return None
        current = self.get_current_document(session_id)
        if isinstance(current, dict) and str(current.get("document_id") or "").strip() == target:
            return current
        for item in self.list_documents(session_id):
            if str(item.get("document_id") or "").strip() == target:
                return item
        return None

    def save_current_document(
        self,
        session_id: str,
        *,
        document_id: str,
        url: str | None,
        title: str,
        episode_id: int | None = None,
        task_run_id: str | None = None,
        version: object = 1,
        sync_mode: str = "created",
        section_snapshot: list[dict[str, Any]] | None = None,
        section_block_index: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "document_id": document_id.strip(),
            "url": (url or "").strip() or None,
            "title": title.strip(),
            "episode_id": episode_id,
            "task_run_id": task_run_id,
            "version": coerce_positive_int(version),
            "sync_mode": sync_mode.strip() or "created",
            "section_snapshot": self.build_section_snapshot(section_snapshot or []),
            "section_block_index": self.build_section_block_index(section_block_index or []),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state_service.set_value(self._key(session_id), json.dumps(payload, ensure_ascii=False))
        self._upsert_document_list(session_id, payload)
        return payload

    def clear_current_document(self, session_id: str) -> None:
        self.state_service.set_value(self._key(session_id), "")

    def build_section_snapshot(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
            if not heading and not cleaned:
                continue
            snapshot.append(
                {
                    "heading": heading,
                    "paragraphs": cleaned,
                }
            )
        return snapshot

    def build_section_block_index(self, block_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in block_index:
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or "").strip()
            block_ids = [
                str(block_id).strip()
                for block_id in item.get("block_ids", [])
                if str(block_id).strip()
            ] if isinstance(item.get("block_ids"), list) else []
            if not heading and not block_ids:
                continue
            result.append(
                {
                    "heading": heading,
                    "start_index": self._safe_index(item.get("start_index")),
                    "end_index": self._safe_index(item.get("end_index")),
                    "block_ids": block_ids,
                }
            )
        return result

    def _safe_index(self, value: Any) -> int:
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _key(self, session_id: str) -> str:
        return f"{self.KEY_PREFIX}:{session_id.strip()}"

    def _list_key(self, session_id: str) -> str:
        return f"{self.LIST_KEY_PREFIX}:{session_id.strip()}"

    def _upsert_document_list(self, session_id: str, payload: dict[str, Any]) -> None:
        existing = self.list_documents(session_id)
        document_id = str(payload.get("document_id") or "").strip()
        updated: list[dict[str, Any]] = []
        replaced = False
        for item in existing:
            if str(item.get("document_id") or "").strip() == document_id:
                updated.append(dict(payload))
                replaced = True
            else:
                item_copy = dict(item)
                item_copy.pop("is_current", None)
                updated.append(item_copy)
        if not replaced:
            updated.append(dict(payload))
        updated.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        self.state_service.set_value(self._list_key(session_id), json.dumps(updated, ensure_ascii=False))
