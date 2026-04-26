from fastapi import APIRouter, HTTPException, Query

from app.schemas.task_run import ConfirmationAnswerRequest, ConfirmationAnswerResponse, TaskRunDetail, TaskRunSummary
from app.services.task_run_service import TaskRunService


router = APIRouter()
task_run_service = TaskRunService()


@router.get("/", response_model=list[TaskRunSummary])
async def list_task_runs(
    session_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[TaskRunSummary]:
    return task_run_service.list_task_runs(session_id=session_id, status=status, limit=limit)


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
    return record
