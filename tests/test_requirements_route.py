import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.requirements import (
    create_requirement,
    get_requirement,
    list_requirement_task_runs,
    list_requirements,
    reassign_task_run_requirement,
)
from app.db.models import Artifact, ConfirmationRequest, Message, Requirement, RequirementSource, TaskRun, TaskRunStep, UserAlias
from app.schemas.requirement import RequirementCreateRequest, RequirementReassignRequest
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
        return f"会话 {session_id}"


class _StubSessionDocumentService:
    def list_documents(self, session_id: str) -> list[dict]:
        return []


class RequirementRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "requirements_route.db")
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
        route_patcher = patch("app.api.routes.requirements.requirement_service", self.requirement_service)
        self.addCleanup(route_patcher.stop)
        route_patcher.start()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_create_list_get_and_reassign_requirement_routes(self) -> None:
        created = asyncio.run(create_requirement(RequirementCreateRequest(
            title="校园活动报名系统",
            primary_session_id="oc_route",
            summary="活动报名、审核和汇报。",
            created_by="tester",
        )))
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_route",
                title="生成报名系统 PPT",
                source_type="group",
            )
        artifact = self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="slides_package",
            title="报名系统 PPT",
            provider="local",
            preview={"slides": []},
        )

        reassigned = asyncio.run(reassign_task_run_requirement(
            created.requirement_id,
            task_run.task_run_id,
            RequirementReassignRequest(requirement_id=created.requirement_id),
        ))
        listed = asyncio.run(list_requirements(session_id="oc_route", query="报名", limit=20))
        detail = asyncio.run(get_requirement(created.requirement_id))
        task_runs = asyncio.run(list_requirement_task_runs(created.requirement_id))

        self.assertEqual(reassigned.requirement_id, created.requirement_id)
        self.assertEqual([item.requirement_id for item in listed], [created.requirement_id])
        self.assertEqual(listed[0].primary_session_label, "会话 oc_route")
        self.assertEqual(listed[0].task_run_count, 1)
        self.assertGreaterEqual(listed[0].source_count, 1)
        self.assertEqual(detail.current_slides_artifact_id, artifact.artifact_id)
        self.assertEqual(detail.task_runs[0].task_run_id, task_run.task_run_id)
        self.assertEqual([item.task_run_id for item in task_runs], [task_run.task_run_id])

    def test_reassign_route_rejects_mismatched_payload(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(reassign_task_run_requirement(
                "req_path",
                "run_missing",
                RequirementReassignRequest(requirement_id="req_other"),
            ))

        self.assertEqual(context.exception.status_code, 400)

    def test_get_requirement_route_returns_404_for_missing_requirement(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(get_requirement("req_missing"))

        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
