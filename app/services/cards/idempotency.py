from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _ActionState:
    status: str
    created_at: float
    result: dict[str, Any] | None = None


class CardActionDedupService:
    """Small in-memory dedup guard for Feishu card callback retries."""

    def __init__(self, *, ttl_seconds: int = 3600) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, _ActionState] = {}

    def accept(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        with self._lock:
            self._cleanup(now)
            state = self._items.get(key)
            if state is not None:
                return False
            self._items[key] = _ActionState(status="processing", created_at=now)
            return True

    def finish(self, key: str, result: dict[str, Any]) -> None:
        if not key:
            return
        with self._lock:
            state = self._items.get(key)
            if state is None:
                self._items[key] = _ActionState(status="done", created_at=time.time(), result=result)
                return
            state.status = "done"
            state.result = result

    def get_result(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._items.get(key)
            return dict(state.result) if state and state.result else None

    def _cleanup(self, now: float) -> None:
        expired = [
            key
            for key, state in self._items.items()
            if now - state.created_at > self.ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)

