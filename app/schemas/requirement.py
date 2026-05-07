from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.next_action import NextActionBundle
from app.schemas.task_run import ArtifactRecord, SessionDocumentRecord, TaskRunSummary


class OfflineSyncRecord(BaseModel):
    submission_id: str
    task_run_id: str
    duplicate_of_submission_id: str | None = None
    requirement_id: str | None = None
    title: str | None = None
    file_name: str | None = None
    file_extension: str | None = None
    status: str
    stage: str | None = None
    latest_summary: str | None = None
    confirmation_id: str | None = None
    confirmation_status: str | None = None
    confirmation_options: list[str] = Field(default_factory=list)
    answer_value: str | None = None
    available_follow_up_targets: list[str] = Field(default_factory=list)
    merge_summary: dict[str, Any] = Field(default_factory=dict)
    merge_plan: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RequirementSummary(BaseModel):
    requirement_id: str
    title: str
    status: str = "active"
    summary: str | None = None
    primary_session_id: str
    primary_session_label: str | None = None
    source_count: int = 0
    task_run_count: int = 0
    latest_source_type: str | None = None
    current_document_id: str | None = None
    current_slides_artifact_id: str | None = None
    current_canvas_artifact_id: str | None = None
    current_delivery_artifact_id: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RequirementSourceRecord(BaseModel):
    source_id: str
    requirement_id: str
    session_id: str
    session_label: str | None = None
    session_type: str | None = None
    message_id: str | None = None
    sender_id: str | None = None
    sender_label: str | None = None
    message_text: str | None = None
    message_status: str | None = None
    source_type: str = "im"
    created_at: datetime | None = None


class RequirementTimelineItem(BaseModel):
    item_id: str
    item_type: str
    title: str
    status: str
    stage: str | None = None
    summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RequirementDetail(RequirementSummary):
    sources: list[RequirementSourceRecord] = Field(default_factory=list)
    task_runs: list[TaskRunSummary] = Field(default_factory=list)
    timeline: list[RequirementTimelineItem] = Field(default_factory=list)
    offline_syncs: list[OfflineSyncRecord] = Field(default_factory=list)
    current_document: SessionDocumentRecord | None = None
    current_slides: ArtifactRecord | None = None
    current_canvas: ArtifactRecord | None = None
    current_delivery: ArtifactRecord | None = None
    recommendations: NextActionBundle | None = None


class RequirementCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    summary: str | None = None
    primary_session_id: str
    created_by: str | None = None


class RequirementUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    summary: str | None = None
    status: str | None = Field(default=None, min_length=1)


class RequirementCurrentProductsRequest(BaseModel):
    current_document_id: str | None = None
    current_slides_artifact_id: str | None = None
    current_canvas_artifact_id: str | None = None
    current_delivery_artifact_id: str | None = None


class RequirementReassignRequest(BaseModel):
    requirement_id: str = Field(min_length=1)


class RequirementResolveCandidate(BaseModel):
    requirement_id: str
    title: str
    summary: str | None = None
    score: float = 0.0
    reason: str = ""
    latest_task_run_id: str | None = None


class RequirementResolveResult(BaseModel):
    action: str
    requirement_id: str | None = None
    confidence: float = 0.0
    matched_by: str = ""
    reason: str = ""
    candidates: list[RequirementResolveCandidate] = Field(default_factory=list)
