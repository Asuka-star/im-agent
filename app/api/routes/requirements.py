import logging

from fastapi import APIRouter, HTTPException, Query

from app.schemas.requirement import (
    RequirementCreateRequest,
    RequirementDetail,
    RequirementReassignRequest,
    RequirementSummary,
)
from app.schemas.task_run import TaskRunSummary
from app.services.requirement_service import RequirementService


logger = logging.getLogger(__name__)
router = APIRouter()
requirement_service = RequirementService()


@router.get("/", response_model=list[RequirementSummary])
async def list_requirements(
    session_id: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RequirementSummary]:
    return requirement_service.list_requirements(
        session_id=session_id,
        query=query,
        limit=limit,
    )


@router.post("/", response_model=RequirementSummary)
async def create_requirement(payload: RequirementCreateRequest) -> RequirementSummary:
    return requirement_service.create_requirement(
        title=payload.title,
        primary_session_id=payload.primary_session_id,
        summary=payload.summary,
        created_by=payload.created_by,
        source_type="manual",
    )


@router.get("/{requirement_id}", response_model=RequirementDetail)
async def get_requirement(requirement_id: str) -> RequirementDetail:
    record = requirement_service.get_requirement(requirement_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return record


@router.get("/{requirement_id}/task-runs", response_model=list[TaskRunSummary])
async def list_requirement_task_runs(requirement_id: str) -> list[TaskRunSummary]:
    record = requirement_service.get_requirement(requirement_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return record.task_runs


@router.post("/{requirement_id}/task-runs/{task_run_id}/reassign", response_model=TaskRunSummary)
async def reassign_task_run_requirement(
    requirement_id: str,
    task_run_id: str,
    payload: RequirementReassignRequest | None = None,
) -> TaskRunSummary:
    target_requirement_id = payload.requirement_id if payload is not None else requirement_id
    if target_requirement_id != requirement_id:
        raise HTTPException(status_code=400, detail="Path requirement_id and payload requirement_id do not match")
    record = requirement_service.bind_task_run(
        task_run_id=task_run_id,
        requirement_id=requirement_id,
        source_type="manual_reassign",
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Requirement or task run not found")
    return record
