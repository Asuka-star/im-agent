import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.offline_syncs import confirm_offline_sync, get_offline_sync
from app.db.models import Artifact, ConfirmationRequest, Message, Requirement, RequirementSource, TaskRun, TaskRunStep, UserAlias
from app.schemas.task_run import OfflineSyncConfirmRequest
from app.services.requirement_service import RequirementService
from app.services.task_run_service import TaskRunService


class _StubSessionDisplayService:
    def resolve_session_label(
        self,
        *,
        session_id: str,
        source_type: str | None = None,
        source_ref: str | None = None,
        created_by: str | None = None,
    ) -> str | None:
        return f"session {session_id}"


class _StubSessionDocumentService:
    def list_documents(self, session_id: str) -> list[dict]:
        return []


class _StubWorkflowService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
        override_instruction: str | None = None,
    ) -> dict:
        payload = {
            "task_run_id": task_run_id,
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
            "override_instruction": override_instruction,
        }
        self.calls.append(payload)
        return payload


class OfflineSyncRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "offline_syncs_route.db")
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Requirement.__table__.create(bind=self.engine)
        RequirementSource.__table__.create(bind=self.engine)
        Message.__table__.create(bind=self.engine)
        UserAlias.__table__.create(bind=self.engine)
        TaskRun.__table__.create(bind=self.engine)
        TaskRunStep.__table__.create(bind=self.engine)
        Artifact.__table__.create(bind=self.engine)
        ConfirmationRequest.__table__.create(bind=self.engine)

        service_patcher = patch("app.services.requirement_service.SessionLocal", self.test_session_local)
        task_run_patcher = patch("app.services.task_run_service.SessionLocal", self.test_session_local)
        self.addCleanup(service_patcher.stop)
        self.addCleanup(task_run_patcher.stop)
        service_patcher.start()
        task_run_patcher.start()

        session_display_service = _StubSessionDisplayService()
        self.task_run_service = TaskRunService(
            session_display_service=session_display_service,
            session_document_service=_StubSessionDocumentService(),
        )
        self.requirement_service = RequirementService(
            task_run_service=self.task_run_service,
            session_document_service=_StubSessionDocumentService(),
            session_display_service=session_display_service,
        )
        self.workflow_service = _StubWorkflowService()

        route_requirement_patcher = patch("app.api.routes.offline_syncs.requirement_service", self.requirement_service)
        route_task_run_patcher = patch("app.api.routes.offline_syncs.task_run_service", self.task_run_service)
        route_workflow_patcher = patch("app.api.routes.offline_syncs.workflow_service", self.workflow_service)
        self.addCleanup(route_requirement_patcher.stop)
        self.addCleanup(route_task_run_patcher.stop)
        self.addCleanup(route_workflow_patcher.stop)
        route_requirement_patcher.start()
        route_task_run_patcher.start()
        route_workflow_patcher.start()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_get_offline_sync_returns_detail_record(self) -> None:
        requirement = self.requirement_service.create_requirement(
            title="Offline Sync Requirement",
            primary_session_id="oc_offline_route",
            summary="queue offline syncs",
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_offline_route",
                title="Import offline doc",
                source_type="file",
                requirement_id=requirement.requirement_id,
            )
        confirmation = self.task_run_service.create_confirmation(
            task_run.task_run_id,
            prompt="Choose how to process the offline document",
            options=["仅更新当前文档", "仅作为参考材料暂存"],
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="offline_document_parsed",
            title="Parsed offline document",
            step_type="offline_document",
            status="done",
            output_payload={"file_name": "meeting.docx", "file_extension": ".docx"},
        )
        self.task_run_service.update_task_run(
            task_run.task_run_id,
            stage="offline_document_confirmation",
            status="waiting_confirmation",
            metadata={
                "offline_document_confirmation": {
                    "confirmation_id": confirmation.confirmation_id,
                    "file_name": "meeting.docx",
                    "file_extension": ".docx",
                    "available_follow_up_targets": ["slides", "canvas"],
                    "merge_summary": {"summary_lines": ["拟更新章节 2 个"]},
                    "merge_plan": {"warning_flags": ["contains_rich_media"]},
                }
            },
        )

        record = asyncio.run(get_offline_sync(task_run.task_run_id))

        self.assertEqual(record.task_run_id, task_run.task_run_id)
        self.assertEqual(record.file_name, "meeting.docx")
        self.assertEqual(record.status, "awaiting_confirmation")
        self.assertEqual(record.confirmation_id, confirmation.confirmation_id)

    def test_confirm_offline_sync_reuses_confirmation_resume_flow(self) -> None:
        requirement = self.requirement_service.create_requirement(
            title="Offline Sync Requirement",
            primary_session_id="oc_offline_confirm",
            summary="confirm offline sync",
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_offline_confirm",
                title="Import offline doc",
                source_type="file",
                requirement_id=requirement.requirement_id,
            )
        confirmation = self.task_run_service.create_confirmation(
            task_run.task_run_id,
            prompt="Choose how to process the offline document",
            options=["仅更新当前文档", "更新文档 + 当前 PPT"],
        )
        self.task_run_service.update_task_run(
            task_run.task_run_id,
            stage="offline_document_confirmation",
            status="waiting_confirmation",
            metadata={
                "offline_document_confirmation": {
                    "confirmation_id": confirmation.confirmation_id,
                    "file_name": "meeting.docx",
                    "file_extension": ".docx",
                    "available_follow_up_targets": ["slides"],
                    "merge_summary": {"summary_lines": ["拟更新章节 1 个"]},
                    "merge_plan": {"warning_flags": []},
                }
            },
        )

        record = asyncio.run(confirm_offline_sync(
            task_run.task_run_id,
            OfflineSyncConfirmRequest(
                answer_value="更新文档 + 当前 PPT",
                answered_by="tester",
                override_instruction="只同步第 3 页",
            ),
        ))

        self.assertEqual(record.task_run_id, task_run.task_run_id)
        self.assertEqual(record.answer_value, "更新文档 + 当前 PPT")
        self.assertEqual(len(self.workflow_service.calls), 1)
        self.assertEqual(self.workflow_service.calls[0]["confirmation_id"], confirmation.confirmation_id)
        self.assertEqual(self.workflow_service.calls[0]["override_instruction"], "只同步第 3 页")

    def test_confirm_offline_sync_rejects_non_pending_record(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(confirm_offline_sync(
                "run_missing",
                OfflineSyncConfirmRequest(answer_value="仅更新当前文档"),
            ))

        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
