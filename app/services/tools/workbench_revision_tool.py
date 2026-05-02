from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.memory_service import MemoryService
from app.services.task_run_service import TaskRunService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkbenchRevisionStart:
    task_run: object
    synthetic_message_id: str


class WorkbenchRevisionTool:
    """Shared setup for Workbench-triggered revision runs."""

    def __init__(self, *, task_run_service: TaskRunService, memory_service: MemoryService) -> None:
        self.task_run_service = task_run_service
        self.memory_service = memory_service

    def start_run(
        self,
        *,
        session_id: str,
        source_task_run_id: str,
        title: str,
        intent: str,
        requested_by: str,
        metadata: dict,
        step_title: str,
        input_payload: dict,
        stage: str,
    ) -> WorkbenchRevisionStart:
        task_run = self.task_run_service.create_task_run(
            session_id=session_id,
            title=title,
            source_type="workbench",
            source_ref=source_task_run_id,
            created_by=requested_by,
            intent=intent,
            metadata=metadata,
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="request_received",
            title=step_title,
            step_type="input",
            status="done",
            input_payload=input_payload,
        )
        self.task_run_service.update_task_run(
            task_run.task_run_id,
            stage=stage,
            status="running",
        )
        return WorkbenchRevisionStart(
            task_run=task_run,
            synthetic_message_id=f"workbench:{task_run.task_run_id}",
        )

    def remember_user_instruction(
        self,
        *,
        session_id: str,
        synthetic_message_id: str,
        requested_by: str,
        instruction: str,
        label: str,
    ) -> None:
        try:
            self.memory_service.save_user_message(
                session_id=session_id,
                message_id=synthetic_message_id,
                sender_id=requested_by,
                content=instruction,
                embed=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to save %s revision user message: %s", label, exc)

    def build_workspace_context(
        self,
        *,
        session_id: str,
        synthetic_message_id: str,
        instruction: str,
        label: str,
    ) -> str:
        try:
            return self.memory_service.build_workspace_context(
                session_id,
                include_pending=True,
                exclude_message_id=synthetic_message_id,
                query_text=instruction,
                include_semantic_search=False,
                episode_id=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to build %s revision workspace context: %s", label, exc)
            return ""
