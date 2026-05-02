from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class NextActionRecommendation(BaseModel):
    action_id: str
    title: str
    description: str = ""
    action_type: str
    priority: str = "medium"
    confidence: float = 0.0
    reason: str = ""
    source: str = "rule"
    requires_confirmation: bool = True
    command: str | None = None
    target_kind: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NextActionBundle(BaseModel):
    task_run_id: str
    session_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str = ""
    recommendations: list[NextActionRecommendation] = Field(default_factory=list)

