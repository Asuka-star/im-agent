from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FeishuCardActionPayload(BaseModel):
    action: str
    version: int = 1
    session_id: str | None = None
    task_run_id: str | None = None
    source_message_id: str | None = None
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class FeishuCardActionEvent(BaseModel):
    event_id: str | None = None
    event_type: str | None = None
    message_id: str | None = None
    chat_id: str | None = None
    operator_id: str | None = None
    action: FeishuCardActionPayload
    raw_payload: dict[str, Any] = Field(default_factory=dict)

