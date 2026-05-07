from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.db.models import AppSetting, Session as SessionModel
from app.services.app_state import AppStateService
from app.services.document_section_utils import build_section_snapshot
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
        if not self._uses_database_state():
            self.state_service.set_value(self._key(session_id), json.dumps(payload, ensure_ascii=False))
            self.state_service.set_value(
                self._list_key(session_id),
                json.dumps(self._merged_document_list(self.list_documents(session_id), payload), ensure_ascii=False),
            )
            return payload
        self._mutate_document_state(
            session_id,
            lambda _current, existing_list: (dict(payload), self._merged_document_list(existing_list, payload), dict(payload)),
        )
        return payload

    def mark_documents_source_dirty(
        self,
        session_id: str,
        *,
        message_id: str,
        episode_id: int | None = None,
        task_run_ids: list[str] | None = None,
        event_type: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        target_task_run_ids = {str(item).strip() for item in (task_run_ids or []) if str(item).strip()}
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "source_dirty": True,
            "source_dirty_message_id": message_id,
            "source_dirty_event_type": event_type,
            "source_dirty_reason": reason,
            "source_dirty_at": now,
        }
        if not self._uses_database_state():
            current = self.get_current_document(session_id)
            list_items = self.list_documents(session_id)
            dirty_documents: list[dict[str, Any]] = []
            updated_list: list[dict[str, Any]] = []
            current_id = str(current.get("document_id") or "").strip() if isinstance(current, dict) else ""
            updated_current: dict[str, Any] | None = None

            for item in list_items:
                document = dict(item)
                document.pop("is_current", None)
                matches_episode = self._matches_episode_id(document.get("episode_id"), episode_id)
                matches_task_run = bool(target_task_run_ids and str(document.get("task_run_id") or "") in target_task_run_ids)
                if matches_episode or matches_task_run:
                    document.update(patch)
                    dirty_documents.append(dict(document))
                if current_id and str(document.get("document_id") or "").strip() == current_id:
                    updated_current = dict(document)
                updated_list.append(document)

            if isinstance(current, dict) and current_id and updated_current is None:
                document = dict(current)
                matches_episode = self._matches_episode_id(document.get("episode_id"), episode_id)
                matches_task_run = bool(target_task_run_ids and str(document.get("task_run_id") or "") in target_task_run_ids)
                if matches_episode or matches_task_run:
                    document.update(patch)
                    dirty_documents.append(dict(document))
                    updated_current = dict(document)

            if updated_current is not None:
                self.state_service.set_value(self._key(session_id), json.dumps(updated_current, ensure_ascii=False))
            if updated_list:
                updated_list.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                self.state_service.set_value(self._list_key(session_id), json.dumps(updated_list, ensure_ascii=False))
            return dirty_documents

        def mutate(current: dict[str, Any] | None, list_items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
            dirty_documents: list[dict[str, Any]] = []
            updated_list: list[dict[str, Any]] = []
            current_id = str(current.get("document_id") or "").strip() if isinstance(current, dict) else ""
            updated_current: dict[str, Any] | None = None

            for item in list_items:
                document = dict(item)
                document.pop("is_current", None)
                matches_episode = self._matches_episode_id(document.get("episode_id"), episode_id)
                matches_task_run = bool(target_task_run_ids and str(document.get("task_run_id") or "") in target_task_run_ids)
                if matches_episode or matches_task_run:
                    document.update(patch)
                    dirty_documents.append(dict(document))
                if current_id and str(document.get("document_id") or "").strip() == current_id:
                    updated_current = dict(document)
                updated_list.append(document)

            if isinstance(current, dict) and current_id and updated_current is None:
                document = dict(current)
                matches_episode = self._matches_episode_id(document.get("episode_id"), episode_id)
                matches_task_run = bool(target_task_run_ids and str(document.get("task_run_id") or "") in target_task_run_ids)
                if matches_episode or matches_task_run:
                    document.update(patch)
                    dirty_documents.append(dict(document))
                    updated_current = dict(document)

            updated_list.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return updated_current, updated_list, dirty_documents

        return self._mutate_document_state(session_id, mutate)

    @staticmethod
    def _matches_episode_id(value: object, episode_id: int | None) -> bool:
        if episode_id is None or value is None:
            return False
        try:
            return int(value) == episode_id
        except (TypeError, ValueError):
            return False

    def clear_current_document(self, session_id: str) -> None:
        if not self._uses_database_state():
            self.state_service.set_value(self._key(session_id), "")
            return
        self._mutate_document_state(session_id, lambda _current, list_items: (None, list_items, None))

    def build_section_snapshot(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return build_section_snapshot(sections)

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

    def _merged_document_list(self, existing: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
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
        return updated

    def _mutate_document_state(self, session_id: str, updater):
        normalized_session_id = session_id.strip()
        while True:
            with SessionLocal() as session:
                if not self._ensure_session_row(session, normalized_session_id):
                    continue
                session.execute(
                    select(SessionModel).where(SessionModel.session_id == normalized_session_id).with_for_update()
                ).scalar_one()
                current_row = session.execute(
                    select(AppSetting).where(AppSetting.key == self._key(normalized_session_id)).with_for_update()
                ).scalar_one_or_none()
                list_row = session.execute(
                    select(AppSetting).where(AppSetting.key == self._list_key(normalized_session_id)).with_for_update()
                ).scalar_one_or_none()

                current = self._decode_document(current_row.value if current_row else None)
                list_items = self._decode_document_list(list_row.value if list_row else None)
                next_current, next_list, result = updater(current, list_items)

                self._write_state_row(session, current_row, self._key(normalized_session_id), next_current)
                self._write_state_row(session, list_row, self._list_key(normalized_session_id), next_list)
                try:
                    session.commit()
                    return result
                except IntegrityError:
                    session.rollback()

    def _uses_database_state(self) -> bool:
        return self.state_service.__class__ is AppStateService

    def _ensure_session_row(self, session, session_id: str) -> None:
        existing = session.execute(
            select(SessionModel.id).where(SessionModel.session_id == session_id)
        ).scalar_one_or_none()
        if existing is not None:
            return True
        session.add(SessionModel(session_id=session_id))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return False
        return True

    @staticmethod
    def _decode_document(raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _decode_document_list(raw: str | None) -> list[dict[str, Any]]:
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _write_state_row(session, row: AppSetting | None, key: str, value: dict[str, Any] | list[dict[str, Any]] | None) -> None:
        serialized = "" if value is None else json.dumps(value, ensure_ascii=False)
        if row is None:
            session.add(AppSetting(key=key, value=serialized))
            return
        row.value = serialized
