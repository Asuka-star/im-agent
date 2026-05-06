from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TaskRunStepRecord(BaseModel):
    step_key: str
    title: str
    step_type: str
    status: str
    input_json: str | None = None
    output_json: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ArtifactRecord(BaseModel):
    artifact_id: str
    artifact_type: str
    provider: str
    title: str
    status: str
    url: str | None = None
    version: int = 1
    preview_json: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ArtifactCheckRecord(BaseModel):
    key: str
    label: str
    status: str
    detail: str
    category: str = "artifact"


class ContextPackItemRecord(BaseModel):
    kind: str
    label: str
    detail: str
    status: str = "ready"
    url: str | None = None


class ContextPackRecord(BaseModel):
    summary: str
    used_sources: list[ContextPackItemRecord] = Field(default_factory=list)
    missing_items: list[ContextPackItemRecord] = Field(default_factory=list)
    suggested_inputs: list[str] = Field(default_factory=list)


class SessionDocumentRecord(BaseModel):
    session_id: str
    document_id: str
    url: str | None = None
    title: str
    version: int = 1
    sync_mode: str
    task_run_id: str | None = None
    updated_at: datetime | None = None
    is_current: bool = False


class ConfirmationRequestRecord(BaseModel):
    confirmation_id: str
    prompt: str
    options_json: str | None = None
    status: str
    answer_value: str | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskRunSummary(BaseModel):
    task_run_id: str
    session_id: str
    session_label: str | None = None
    source_type: str
    source_ref: str | None = None
    trigger_message_id: str | None = None
    intent: str | None = None
    title: str
    stage: str
    status: str
    latest_summary: str | None = None
    latest_reply_preview: str | None = None
    latest_error: str | None = None
    run_kind: str | None = None
    primary_object: str | None = None
    lifecycle_stage: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class TaskRunDetail(TaskRunSummary):
    metadata_json: str | None = None
    graph_trace: dict[str, Any] | None = None
    steps: list[TaskRunStepRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    artifact_checks: list[ArtifactCheckRecord] = Field(default_factory=list)
    context_pack: ContextPackRecord | None = None
    confirmations: list[ConfirmationRequestRecord] = Field(default_factory=list)
    session_documents: list[SessionDocumentRecord] = Field(default_factory=list)


class ConfirmationAnswerRequest(BaseModel):
    confirmation_id: str
    answer_value: str
    answered_by: str = Field(default="user", description="Who resolved the confirmation")


class ConfirmationAnswerResponse(BaseModel):
    task_run_id: str
    confirmation_id: str
    status: str
    answer_value: str
    already_answered: bool = False


class DocumentRevisionRequest(BaseModel):
    instruction: str = Field(min_length=1, description="Natural-language instruction for revising the current document")
    requested_by: str = Field(default="pilot_workbench", description="Who requested the document revision")
    document_id: str | None = Field(default=None, description="Optional explicit target document id within the session")


class SlidesRevisionRequest(BaseModel):
    instruction: str = Field(min_length=1, description="Natural-language instruction for revising the current slides package")
    requested_by: str = Field(default="pilot_workbench", description="Who requested the slides revision")
    artifact_id: str | None = Field(default=None, description="Optional explicit slides artifact id to revise")


class DeliveryBundleRequest(BaseModel):
    requested_by: str = Field(default="pilot_workbench", description="Who requested the delivery bundle")
