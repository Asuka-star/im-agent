from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class TaskRunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    TaskRunStatus.COMPLETED,
    TaskRunStatus.COMPLETED_WITH_WARNINGS,
    TaskRunStatus.PARTIAL_FAILED,
    TaskRunStatus.FAILED,
    TaskRunStatus.CANCELLED,
}


KNOWN_STATUSES = {
    TaskRunStatus.QUEUED,
    TaskRunStatus.RUNNING,
    TaskRunStatus.WAITING_CONFIRMATION,
    *TERMINAL_STATUSES,
}


ALLOWED_TRANSITIONS = {
    TaskRunStatus.QUEUED: {
        TaskRunStatus.QUEUED,
        TaskRunStatus.RUNNING,
        TaskRunStatus.WAITING_CONFIRMATION,
        TaskRunStatus.COMPLETED,
        TaskRunStatus.COMPLETED_WITH_WARNINGS,
        TaskRunStatus.PARTIAL_FAILED,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
    },
    TaskRunStatus.RUNNING: {
        TaskRunStatus.RUNNING,
        TaskRunStatus.WAITING_CONFIRMATION,
        TaskRunStatus.COMPLETED,
        TaskRunStatus.COMPLETED_WITH_WARNINGS,
        TaskRunStatus.PARTIAL_FAILED,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
    },
    TaskRunStatus.WAITING_CONFIRMATION: {
        TaskRunStatus.WAITING_CONFIRMATION,
        TaskRunStatus.RUNNING,
        TaskRunStatus.COMPLETED,
        TaskRunStatus.COMPLETED_WITH_WARNINGS,
        TaskRunStatus.PARTIAL_FAILED,
        TaskRunStatus.FAILED,
        TaskRunStatus.CANCELLED,
    },
    TaskRunStatus.COMPLETED: {TaskRunStatus.COMPLETED},
    TaskRunStatus.COMPLETED_WITH_WARNINGS: {TaskRunStatus.COMPLETED_WITH_WARNINGS},
    TaskRunStatus.PARTIAL_FAILED: {TaskRunStatus.PARTIAL_FAILED},
    TaskRunStatus.FAILED: {TaskRunStatus.FAILED},
    TaskRunStatus.CANCELLED: {TaskRunStatus.CANCELLED},
}


@dataclass(frozen=True)
class TaskRunTransition:
    current_status: str
    requested_status: str
    accepted: bool
    reason: str = ""
    completed_at: datetime | None = None


def normalize_task_run_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    aliases = {
        "done": TaskRunStatus.COMPLETED,
        "success": TaskRunStatus.COMPLETED,
        "succeeded": TaskRunStatus.COMPLETED,
        "complete": TaskRunStatus.COMPLETED,
        "error": TaskRunStatus.FAILED,
        "failure": TaskRunStatus.FAILED,
        "cancel": TaskRunStatus.CANCELLED,
        "canceled": TaskRunStatus.CANCELLED,
        "warning": TaskRunStatus.COMPLETED_WITH_WARNINGS,
        "warnings": TaskRunStatus.COMPLETED_WITH_WARNINGS,
        "partial": TaskRunStatus.PARTIAL_FAILED,
        "partial_failure": TaskRunStatus.PARTIAL_FAILED,
    }
    return aliases.get(normalized, normalized)


def transition_task_run_status(current_status: str | None, requested_status: str | None) -> TaskRunTransition:
    current = normalize_task_run_status(current_status) or TaskRunStatus.QUEUED
    requested = normalize_task_run_status(requested_status) or current
    if requested not in KNOWN_STATUSES:
        return TaskRunTransition(
            current_status=current,
            requested_status=requested,
            accepted=False,
            reason=f"unknown task run status: {requested}",
        )
    allowed = ALLOWED_TRANSITIONS.get(current, {current})
    if requested not in allowed:
        return TaskRunTransition(
            current_status=current,
            requested_status=requested,
            accepted=False,
            reason=f"illegal task run status transition: {current} -> {requested}",
        )
    completed_at = datetime.now(timezone.utc) if requested in TERMINAL_STATUSES else None
    return TaskRunTransition(
        current_status=current,
        requested_status=requested,
        accepted=True,
        completed_at=completed_at,
    )
