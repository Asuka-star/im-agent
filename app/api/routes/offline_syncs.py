import logging

from fastapi import APIRouter, HTTPException

from app.schemas.requirement import OfflineSyncRecord
from app.schemas.task_run import ConfirmationAnswerResponse, OfflineSyncConfirmRequest
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.requirement_service import RequirementService
from app.services.task_run_service import TaskRunService


logger = logging.getLogger(__name__)
router = APIRouter()
requirement_service = RequirementService()
task_run_service = TaskRunService()
workflow_service = FeishuWorkflowService()


@router.get("/{submission_id}", response_model=OfflineSyncRecord)
async def get_offline_sync(submission_id: str) -> OfflineSyncRecord:
    record = requirement_service.get_offline_sync(submission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Offline sync not found")
    return record


@router.post("/{submission_id}/confirm", response_model=ConfirmationAnswerResponse)
async def confirm_offline_sync(submission_id: str, payload: OfflineSyncConfirmRequest) -> ConfirmationAnswerResponse:
    record = requirement_service.get_offline_sync(submission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Offline sync not found")
    if not record.confirmation_id:
        raise HTTPException(status_code=400, detail="Offline sync confirmation is not pending")

    answered = task_run_service.resolve_confirmation(
        record.task_run_id,
        confirmation_id=record.confirmation_id,
        answer_value=payload.answer_value,
        answered_by=payload.answered_by,
    )
    if answered is None:
        raise HTTPException(status_code=404, detail="Confirmation request not found")
    if answered.already_answered:
        return answered
    try:
        resumed = workflow_service.resume_task_run_after_confirmation(
            record.task_run_id,
            confirmation_id=record.confirmation_id,
            answer_value=payload.answer_value,
            answered_by=payload.answered_by,
            override_instruction=payload.override_instruction,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to resume offline sync after confirmation: %s", exc)
        task_run_service.update_task_run(
            record.task_run_id,
            stage="confirmation_resume_failed",
            status="failed",
            latest_error=str(exc),
        )
        raise HTTPException(status_code=500, detail="Failed to resume offline sync after confirmation") from exc
    if resumed is None:
        task_run_service.update_task_run(
            record.task_run_id,
            stage="confirmation_resume_skipped",
            latest_error="No resumable offline sync payload was found for this task run.",
        )
    return answered
