from datetime import datetime

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
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class TaskRunDetail(TaskRunSummary):
    metadata_json: str | None = None
    steps: list[TaskRunStepRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    confirmations: list[ConfirmationRequestRecord] = Field(default_factory=list)


class ConfirmationAnswerRequest(BaseModel):
    confirmation_id: str
    answer_value: str
    answered_by: str = Field(default="user", description="Who resolved the confirmation")


class ConfirmationAnswerResponse(BaseModel):
    task_run_id: str
    confirmation_id: str
    status: str
    answer_value: str
