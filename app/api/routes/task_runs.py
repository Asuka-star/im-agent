import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.schemas.task_run import (
    ConfirmationAnswerRequest,
    ConfirmationAnswerResponse,
    DocumentRevisionRequest,
    TaskRunDetail,
    TaskRunSummary,
)
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.task_run_service import TaskRunService


logger = logging.getLogger(__name__)
router = APIRouter()
task_run_service = TaskRunService()
workflow_service = FeishuWorkflowService()


@router.get("/", response_model=list[TaskRunSummary])
async def list_task_runs(
    session_id: str | None = Query(default=None),
    session_query: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[TaskRunSummary]:
    return task_run_service.list_task_runs(
        session_id=session_id,
        session_query=session_query,
        status=status,
        limit=limit,
    )


@router.get("/{task_run_id}", response_model=TaskRunDetail)
async def get_task_run(task_run_id: str) -> TaskRunDetail:
    record = task_run_service.get_task_run(task_run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task run not found")
    return record


@router.post("/{task_run_id}/confirm", response_model=ConfirmationAnswerResponse)
async def confirm_task_run(task_run_id: str, payload: ConfirmationAnswerRequest) -> ConfirmationAnswerResponse:
    record = task_run_service.resolve_confirmation(
        task_run_id,
        confirmation_id=payload.confirmation_id,
        answer_value=payload.answer_value,
        answered_by=payload.answered_by,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Confirmation request not found")
    try:
        workflow_service.resume_task_run_after_confirmation(
            task_run_id,
            confirmation_id=payload.confirmation_id,
            answer_value=payload.answer_value,
            answered_by=payload.answered_by,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to resume task run after confirmation: %s", exc)
    return record


@router.post("/{task_run_id}/revise-document", response_model=TaskRunDetail)
async def revise_task_run_document(task_run_id: str, payload: DocumentRevisionRequest) -> TaskRunDetail:
    try:
        record = await run_in_threadpool(
            workflow_service.revise_document_from_task_run,
            task_run_id,
            instruction=payload.instruction,
            requested_by=payload.requested_by,
            document_id=payload.document_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to revise document from task run: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to revise document") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Task run not found")
    return record
