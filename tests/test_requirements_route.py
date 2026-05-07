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
    list_requirement_offline_syncs,
    list_requirement_task_runs,
    list_requirements,
    reassign_task_run_requirement,
    update_requirement,
    update_requirement_current_products,
)
from app.db.models import Artifact, ConfirmationRequest, Message, Requirement, RequirementSource, TaskRun, TaskRunStep, UserAlias
from app.schemas.requirement import (
    RequirementCreateRequest,
    RequirementCurrentProductsRequest,
    RequirementReassignRequest,
    RequirementUpdateRequest,
)
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
    def __init__(self, documents: list[dict] | None = None) -> None:
        self.documents = documents or []

    def list_documents(self, session_id: str) -> list[dict]:
        return [item for item in self.documents if item.get("session_id") == session_id]


class _StubFeishuArtifactIntegrator:
    def __init__(self) -> None:
        self.synced: list[object] = []

    def sync_requirement_current_artifacts(self, record: object) -> dict:
        self.synced.append(record)
        return {"status": "ready"}


class _StubWorkflowService:
    def __init__(self) -> None:
        self.feishu_artifact_integrator = _StubFeishuArtifactIntegrator()


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
        self.session_document_service = _StubSessionDocumentService()
        self.task_run_service = TaskRunService(
            session_display_service=session_display_service,
            session_document_service=self.session_document_service,
        )
        self.requirement_service = RequirementService(
            task_run_service=self.task_run_service,
            session_document_service=self.session_document_service,
            session_display_service=session_display_service,
        )
        self.workflow_service = _StubWorkflowService()
        self.requirement_service.current_artifact_syncer = self.workflow_service.feishu_artifact_integrator
        route_patcher = patch("app.api.routes.requirements.requirement_service", self.requirement_service)
        workflow_patcher = patch("app.api.routes.requirements.workflow_service", self.workflow_service)
        self.addCleanup(route_patcher.stop)
        self.addCleanup(workflow_patcher.stop)
        route_patcher.start()
        workflow_patcher.start()

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

    def test_update_requirement_route_supports_lightweight_edits(self) -> None:
        created = asyncio.run(create_requirement(RequirementCreateRequest(
            title="原需求标题",
            primary_session_id="oc_route_edit",
            summary="旧摘要",
        )))

        updated = asyncio.run(update_requirement(
            created.requirement_id,
            RequirementUpdateRequest(title="新需求标题", summary="新摘要", status="paused"),
        ))

        self.assertEqual(updated.title, "新需求标题")
        self.assertEqual(updated.summary, "新摘要")
        self.assertEqual(updated.status, "paused")

    def test_update_requirement_current_products_route_validates_membership(self) -> None:
        created = asyncio.run(create_requirement(RequirementCreateRequest(
            title="当前产物切换",
            primary_session_id="oc_route_products",
            summary="验证手动切换当前产物。",
        )))
        self.session_document_service.documents.append(
            {
                "session_id": "oc_route_products",
                "document_id": "doc_route_manual",
                "title": "Route 文档",
                "version": 2,
                "sync_mode": "manual",
                "is_current": True,
            }
        )
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_route_products",
                title="生成 PPT",
                source_type="group",
                requirement_id=created.requirement_id,
            )
        artifact = self.task_run_service.create_artifact(
            task_run.task_run_id,
            artifact_type="slides_package",
            title="Route PPT",
            provider="local",
            preview={"slides": []},
        )

        detail = asyncio.run(update_requirement_current_products(
            created.requirement_id,
            RequirementCurrentProductsRequest(
                current_document_id="doc_route_manual",
                current_slides_artifact_id=artifact.artifact_id,
            ),
        ))

        self.assertEqual(detail.current_document_id, "doc_route_manual")
        self.assertEqual(detail.current_slides_artifact_id, artifact.artifact_id)
        self.assertEqual(len(self.workflow_service.feishu_artifact_integrator.synced), 1)
        self.assertEqual(
            self.workflow_service.feishu_artifact_integrator.synced[0].requirement_id,
            created.requirement_id,
        )

        with self.assertRaises(HTTPException) as context:
            asyncio.run(update_requirement_current_products(
                created.requirement_id,
                RequirementCurrentProductsRequest(current_canvas_artifact_id="artifact_missing"),
            ))
        self.assertEqual(context.exception.status_code, 400)

    def test_list_requirement_offline_syncs_route_returns_requirement_records(self) -> None:
        created = asyncio.run(create_requirement(RequirementCreateRequest(
            title="离线协作文档",
            primary_session_id="oc_route_offline",
            summary="验证 requirement 视角下的离线回传队列。",
        )))
        with patch("app.services.task_run_service.realtime_hub.emit_room"):
            task_run = self.task_run_service.create_task_run(
                session_id="oc_route_offline",
                title="导入离线纪要",
                source_type="file",
                requirement_id=created.requirement_id,
            )
        confirmation = self.task_run_service.create_confirmation(
            task_run.task_run_id,
            prompt="请选择处理方式",
            options=["仅更新当前文档", "仅作为参考材料暂存"],
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="offline_document_parsed",
            title="解析离线文档",
            step_type="offline_document",
            status="done",
            output_payload={"file_name": "meeting.docx", "file_extension": ".docx"},
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="offline_document_confirmation",
            title="确认离线文档处理方式",
            step_type="confirmation",
            status="pending",
            output_payload={
                "merge_summary": {"summary_lines": ["拟更新章节 2 个"]},
                "merge_plan": {"warning_flags": ["contains_rich_media"]},
            },
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
                    "merge_summary": {"summary_lines": ["拟更新章节 2 个"]},
                    "merge_plan": {"warning_flags": ["contains_rich_media"]},
                }
            },
        )

        records = asyncio.run(list_requirement_offline_syncs(created.requirement_id))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task_run_id, task_run.task_run_id)
        self.assertEqual(records[0].status, "awaiting_confirmation")
        self.assertEqual(records[0].confirmation_id, confirmation.confirmation_id)
        self.assertEqual(records[0].confirmation_options, ["仅更新当前文档", "仅作为参考材料暂存"])

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
