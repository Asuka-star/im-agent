import logging
import threading
import time

from app.feishu.chat_api import FeishuChatAPI
from app.feishu.user_api import FeishuUserAPI
from app.services.memory_service import MemoryService


logger = logging.getLogger(__name__)


class SessionDisplayService:
    """Resolves human-friendly labels for task-run sessions."""

    def __init__(
        self,
        *,
        memory_service: MemoryService | None = None,
        user_api: FeishuUserAPI | None = None,
        chat_api: FeishuChatAPI | None = None,
    ) -> None:
        self.memory_service = memory_service or MemoryService()
        self.user_api = user_api or FeishuUserAPI()
        self.chat_api = chat_api or FeishuChatAPI()
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._cache_ttl_seconds = 300.0

    def resolve_session_label(
        self,
        *,
        session_id: str,
        source_type: str | None = None,
        source_ref: str | None = None,
        created_by: str | None = None,
    ) -> str | None:
        normalized_session_id = (session_id or "").strip()
        normalized_source_type = (source_type or "").strip().lower()
        normalized_source_ref = (source_ref or "").strip()
        normalized_created_by = (created_by or "").strip()

        group_chat_id = normalized_source_ref or normalized_session_id
        if normalized_source_type == "group" and group_chat_id:
            label = self._resolve_group_label(group_chat_id)
            if label:
                return label

        if normalized_source_type == "p2p" and normalized_created_by:
            label = self._resolve_user_label(normalized_created_by)
            if label:
                return label
        if normalized_created_by:
            label = self.memory_service.get_alias_display_name(
                normalized_session_id,
                normalized_created_by,
            )
            if label:
                return label

        return None

    def _resolve_group_label(self, chat_id: str) -> str | None:
        cache_key = f"group:{chat_id}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        try:
            label = self.chat_api.get_chat_name(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve chat name for %s: %s", chat_id, exc)
            return None

        if label:
            self._set_cache(cache_key, label)
        return label

    def _resolve_user_label(self, identifier: str) -> str | None:
        cache_key = f"user:{identifier}"
        cached = self._get_cache(cache_key)
        if cached:
            return cached

        label = None
        for kwargs in ({"user_id": identifier}, {"open_id": identifier}):
            try:
                label = self.user_api.get_user_display_name(**kwargs)
            except Exception:  # noqa: BLE001
                label = None
            if label:
                break

        if label:
            self._set_cache(cache_key, label)
        return label

    def _get_cache(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            value, expires_at = cached
            if expires_at > now:
                return value
            self._cache.pop(key, None)
            return None

    def _set_cache(self, key: str, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            return
        with self._lock:
            self._cache[key] = (
                normalized,
                time.monotonic() + self._cache_ttl_seconds,
            )
