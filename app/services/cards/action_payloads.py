from __future__ import annotations

import hashlib
import json
from typing import Any

from app.schemas.feishu_card import FeishuCardActionPayload


def build_card_action_payload(
    action: str,
    *,
    session_id: str | None = None,
    task_run_id: str | None = None,
    source_message_id: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    body = payload or {}
    key = idempotency_key or _stable_idempotency_key(
        action,
        session_id=session_id,
        task_run_id=task_run_id,
        source_message_id=source_message_id,
        payload=body,
    )
    return FeishuCardActionPayload(
        action=action,
        session_id=session_id,
        task_run_id=task_run_id,
        source_message_id=source_message_id,
        idempotency_key=key,
        payload=body,
    ).model_dump(exclude_none=True)


def _stable_idempotency_key(
    action: str,
    *,
    session_id: str | None,
    task_run_id: str | None,
    source_message_id: str | None,
    payload: dict[str, Any],
) -> str:
    raw = json.dumps(
        {
            "action": action,
            "session_id": session_id,
            "task_run_id": task_run_id,
            "source_message_id": source_message_id,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"card:{action}:{digest}"

