import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Artifact, ConfirmationRequest, Requirement, TaskRun, TaskRunStep
from app.services.task_run_service import TaskRunService


class _StubSessionDisplayService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def resolve_session_label(
        self,
        *,
        session_id: str,
        source_type: str | None = None,
        source_ref: str | None = None,
        created_by: str | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "session_id": session_id,
                "source_type": source_type,
                "source_ref": source_ref,
                "created_by": created_by,
            }
        )
        if source_type == "group":
            return "产品讨论群"
        if source_type == "p2p":
            return "张三"
        return None


class _StubSessionDocumentService:
    def __init__(self, documents: list[dict] | None = None, error: Exception | None = None) -> None:
        self.documents = documents or []
        self.error = error

    def list_documents(self, session_id: str) -> list[dict]:
        if self.error is not None:
            raise self.error
        return self.documents


class _MappedSessionDocumentService:
    def __init__(self, documents_by_session: dict[str, list[dict]]) -> None:
        self.documents_by_session = documents_by_session

    def list_documents(self, session_id: str) -> list[dict]:
        return list(self.documents_by_session.get(session_id, []))


class TaskRunServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "task_runs.db")
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        TaskRun.__table__.create(bind=self.engine)
        TaskRunStep.__table__.create(bind=self.engine)
        Artifact.__table__.create(bind=self.engine)
        ConfirmationRequest.__table__.create(bind=self.engine)
        Requirement.__table__.create(bind=self.engine)

        patcher = patch("app.services.task_run_service.SessionLocal", self.test_session_local)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.session_display_service = _StubSessionDisplayService()
        self.service = TaskRunService(
            session_display_service=self.session_display_service,
            session_document_service=_StubSessionDocumentService(),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_create_run_and_fetch_detail(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="整理需求文档",
            source_type="group",
            source_ref="chat_123",
            created_by="user_1",
            metadata={
                "chat_id": "chat_123",
                "run_kind": "artifact_lifecycle",
                "primary_object": "doc",
                "lifecycle_stage": "document",
            },
        )
        self.service.upsert_step(
            created.task_run_id,
            step_key="request_received",
            title="接收用户请求",
            status="done",
            input_payload={"text": "@机器人 帮我整理文档"},
        )
        self.service.create_artifact(
            created.task_run_id,
            artifact_type="document",
            title="需求文档",
            provider="feishu_doc",
            url="https://feishu.cn/docx/demo",
            preview={"title": "需求文档"},
        )
        self.service.create_confirmation(
            created.task_run_id,
            prompt="是否继续生成汇报稿？",
            options=["继续", "稍后"],
        )

        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.task_run_id, created.task_run_id)
        self.assertEqual(detail.session_id, "oc_test")
        self.assertEqual(detail.session_label, "产品讨论群")
        self.assertEqual(detail.run_kind, "artifact_lifecycle")
        self.assertEqual(detail.primary_object, "doc")
        self.assertEqual(detail.lifecycle_stage, "document")
        self.assertEqual(len(detail.steps), 1)
        self.assertEqual(detail.steps[0].step_key, "request_received")
        self.assertEqual(len(detail.artifacts), 1)
        self.assertEqual(detail.artifacts[0].artifact_type, "document")
        self.assertTrue(detail.artifact_checks)
        check_status = {item.key: item.status for item in detail.artifact_checks}
        self.assertEqual(check_status["document"], "ready")
        self.assertIn("canvas", check_status)
        self.assertIsNotNone(detail.context_pack)
        assert detail.context_pack is not None
        self.assertTrue(any(item.kind == "im" for item in detail.context_pack.used_sources))
        self.assertTrue(any(item.kind == "document" for item in detail.context_pack.used_sources))
        self.assertTrue(any(item.kind == "canvas" for item in detail.context_pack.missing_items))
        self.assertEqual(len(detail.confirmations), 1)
        self.assertEqual(detail.confirmations[0].status, "pending")

    def test_detail_includes_recent_session_artifacts_for_lifecycle_context(self) -> None:
        slides_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            title="生成答辩 PPT",
            source_type="group",
        )
        self.service.create_artifact(
            slides_run.task_run_id,
            artifact_type="slides_package",
            title="校园活动报名系统答辩 PPT",
            url="/api/artifacts/slides/demo.html",
            preview={"exports": {"html": "/api/artifacts/slides/demo.html", "pptx": "/api/artifacts/slides/demo.pptx"}},
        )
        canvas_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            title="生成产品流程图",
            source_type="group",
        )
        self.service.create_artifact(
            canvas_run.task_run_id,
            artifact_type="canvas",
            title="产品流程图",
            url="/api/artifacts/canvas/demo.html",
            preview={"schema": "im-agent.canvas.v1", "shapes": [{"type": "node"}], "exports": {"json": "a", "svg": "b", "html": "c"}},
        )

        detail = self.service.get_task_run(canvas_run.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        artifact_types = [artifact.artifact_type for artifact in detail.artifacts]
        self.assertIn("slides_package", artifact_types)
        self.assertIn("canvas", artifact_types)
        check_status = {item.key: item.status for item in detail.artifact_checks}
        self.assertNotEqual(check_status["slides"], "missing")
        self.assertNotIn("演示稿", [item.label for item in detail.context_pack.missing_items])

    def test_requirement_task_run_detail_does_not_reuse_other_requirement_artifacts(self) -> None:
        with self.test_session_local() as session:
            session.add_all(
                [
                    Requirement(
                        requirement_id="req_activity",
                        title="校园活动报名与审核系统",
                        primary_session_id="oc_lifecycle",
                    ),
                    Requirement(
                        requirement_id="req_lab",
                        title="实验室设备预约系统",
                        primary_session_id="oc_lifecycle",
                    ),
                ]
            )
            session.commit()

        slides_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_activity",
            title="生成答辩 PPT",
            source_type="group",
        )
        self.service.create_artifact(
            slides_run.task_run_id,
            artifact_type="slides_package",
            title="校园活动报名系统答辩 PPT",
            url="/api/artifacts/slides/activity.html",
            preview={"slides": [{"title": "开场"}], "exports": {"html": "a", "pptx": "b"}},
        )
        canvas_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_lab",
            title="生成产品流程图",
            source_type="group",
        )
        self.service.create_artifact(
            canvas_run.task_run_id,
            artifact_type="canvas",
            title="实验室设备预约系统需求流程图",
            url="/api/artifacts/canvas/lab.html",
            preview={"schema": "im-agent.canvas.v1", "shapes": [{"type": "node"}], "exports": {"json": "a", "svg": "b", "html": "c"}},
        )

        activity_detail = self.service.get_task_run(slides_run.task_run_id)
        lab_detail = self.service.get_task_run(canvas_run.task_run_id)

        self.assertIsNotNone(activity_detail)
        self.assertIsNotNone(lab_detail)
        assert activity_detail is not None
        assert lab_detail is not None
        activity_checks = {item.key: item.status for item in activity_detail.artifact_checks}
        lab_checks = {item.key: item.status for item in lab_detail.artifact_checks}
        self.assertEqual(activity_checks["slides"], "ready")
        self.assertEqual(activity_checks["canvas"], "missing")
        self.assertEqual(lab_checks["canvas"], "ready")
        self.assertEqual(lab_checks["slides"], "missing")

    def test_requirement_task_run_detail_exposes_only_current_slides_artifact(self) -> None:
        with self.test_session_local() as session:
            session.add(
                Requirement(
                    requirement_id="req_current_slides",
                    title="校园活动报名系统",
                    primary_session_id="oc_lifecycle",
                )
            )
            session.commit()
        first_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_current_slides",
            title="生成答辩 PPT",
            source_type="group",
        )
        first_artifact = self.service.create_artifact(
            first_run.task_run_id,
            artifact_type="slides_package",
            title="答辩 PPT v1",
            url="/api/artifacts/slides/v1.html",
            preview={"version": 1, "exports": {"html": "v1", "pptx": "v1.pptx"}},
            version=1,
        )
        second_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_current_slides",
            title="更新答辩 PPT",
            source_type="group",
        )
        second_artifact = self.service.create_artifact(
            second_run.task_run_id,
            artifact_type="slides_package",
            title="答辩 PPT v2",
            url="/api/artifacts/slides/v2.html",
            preview={"version": 2, "exports": {"html": "v2", "pptx": "v2.pptx"}},
            version=2,
        )
        with self.test_session_local() as session:
            requirement = session.query(Requirement).filter_by(requirement_id="req_current_slides").one()
            requirement.current_slides_artifact_id = second_artifact.artifact_id
            session.commit()

        detail = self.service.get_task_run(first_run.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        slides = [artifact for artifact in detail.artifacts if artifact.artifact_type == "slides_package"]
        self.assertEqual([artifact.artifact_id for artifact in slides], [second_artifact.artifact_id])
        self.assertNotIn(first_artifact.artifact_id, [artifact.artifact_id for artifact in detail.artifacts])
        self.assertEqual(slides[0].version, 2)

    def test_requirement_task_run_detail_falls_back_to_latest_artifact_when_current_pointer_missing(self) -> None:
        with self.test_session_local() as session:
            session.add(
                Requirement(
                    requirement_id="req_latest_canvas",
                    title="实验室预约系统",
                    primary_session_id="oc_lifecycle",
                )
            )
            session.commit()
        first_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_latest_canvas",
            title="生成 Canvas",
            source_type="group",
        )
        first_artifact = self.service.create_artifact(
            first_run.task_run_id,
            artifact_type="canvas",
            title="流程图 v1",
            url="/api/artifacts/canvas/v1.html",
            preview={"version": 1, "exports": {"html": "v1", "svg": "v1.svg"}},
            version=1,
        )
        second_run = self.service.create_task_run(
            session_id="oc_lifecycle",
            requirement_id="req_latest_canvas",
            title="更新 Canvas",
            source_type="group",
        )
        second_artifact = self.service.create_artifact(
            second_run.task_run_id,
            artifact_type="canvas",
            title="流程图 v2",
            url="/api/artifacts/canvas/v2.html",
            preview={"version": 2, "exports": {"html": "v2", "svg": "v2.svg"}},
            version=2,
        )

        detail = self.service.get_task_run(first_run.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        canvas = [artifact for artifact in detail.artifacts if artifact.artifact_type == "canvas"]
        self.assertEqual([artifact.artifact_id for artifact in canvas], [second_artifact.artifact_id])
        self.assertNotIn(first_artifact.artifact_id, [artifact.artifact_id for artifact in detail.artifacts])

    def test_requirement_task_run_detail_can_use_document_from_other_bound_session(self) -> None:
        self.service.session_document_service = _MappedSessionDocumentService(
            {
                "oc_group": [],
                "ou_personal": [
                    {
                        "session_id": "ou_personal",
                        "document_id": "doc_p2p",
                        "title": "个人单聊生成的需求方案",
                        "url": "https://feishu.cn/docx/doc_p2p",
                        "version": 1,
                        "sync_mode": "created",
                        "task_run_id": "run_doc",
                    }
                ],
            }
        )
        with self.test_session_local() as session:
            session.add(
                Requirement(
                    requirement_id="req_cross_session",
                    title="校园活动报名系统",
                    primary_session_id="oc_group",
                    current_document_id="doc_p2p",
                )
            )
            session.commit()
        group_run = self.service.create_task_run(
            session_id="oc_group",
            requirement_id="req_cross_session",
            title="整理需求",
            source_type="group",
        )
        doc_run = self.service.create_task_run(
            session_id="ou_personal",
            requirement_id="req_cross_session",
            title="生成需求文档",
            source_type="p2p",
        )
        with self.test_session_local() as session:
            row = session.query(TaskRun).filter_by(task_run_id=doc_run.task_run_id).one()
            row.task_run_id = "run_doc"
            session.commit()

        detail = self.service.get_task_run(group_run.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual([document.document_id for document in detail.session_documents], ["doc_p2p"])
        checks = {item.key: item.status for item in detail.artifact_checks}
        self.assertEqual(checks["document"], "ready")

    def test_detail_derives_langgraph_trace_from_metadata_and_worker_steps(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_graph",
            title="Graph run",
            source_type="group",
            metadata={
                "langgraph_execution": {
                    "command": {
                        "mode": "workspace_action",
                        "operation": "generate",
                        "object": "workspace",
                        "requested_outputs": ["doc", "slides"],
                        "confidence": 0.91,
                    },
                    "plan": {
                        "plan_id": "plan_1",
                        "steps": [
                            {
                                "step_id": "doc_generate",
                                "worker": "doc",
                                "operation": "generate",
                                "input": {"agent": "DocAgent", "goal": "doc goal"},
                                "can_run_parallel": True,
                            },
                            {
                                "step_id": "slides_generate",
                                "worker": "slides",
                                "operation": "generate",
                                "can_run_parallel": True,
                            },
                        ],
                    },
                    "worker_results": {
                        "doc_generate": {
                            "worker": "doc",
                            "ok": True,
                            "status": "done",
                            "elapsed_ms": 12.5,
                            "output": {"reply_preview": "doc ready"},
                        }
                    },
                    "review": {
                        "ok": True,
                        "needs_clarification": False,
                        "checks": [
                            {
                                "agent": "ValidatorAgent",
                                "status": "passed",
                                "ok": True,
                                "summary": "All planned graph worker steps completed.",
                                "risks": [],
                            },
                            {
                                "agent": "ShieldAgent",
                                "status": "passed",
                                "ok": True,
                                "summary": "No risky graph operation detected.",
                                "risks": [],
                            },
                        ],
                    },
                    "reply": {"text": "all ready"},
                    "comparison": {
                        "status": "match",
                        "route_match": True,
                        "outputs_match": True,
                        "needs_clarification_match": True,
                        "legacy_route": "doc",
                        "graph_route": "doc",
                        "legacy_outputs": ["doc", "slides"],
                        "graph_outputs": ["doc", "slides"],
                        "legacy_confidence": 0.9,
                        "graph_confidence": 0.91,
                        "confidence_delta": 0.01,
                    },
                    "trace": [{"node": "graph.planner", "status": "done"}],
                    "errors": [],
                }
            },
        )
        self.service.upsert_step(
            created.task_run_id,
            step_key="graph.worker.slides_generate",
            title="LangGraph worker slides",
            step_type="graph_worker",
            status="done",
            output_payload={
                "step_id": "slides_generate",
                "worker": "slides",
                "operation": "generate",
                "elapsed_ms": 28,
                "output": {
                    "reply_preview": "slides ready",
                    "artifacts": [{"artifact_type": "slides"}],
                },
            },
        )

        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNotNone(detail.graph_trace)
        assert detail.graph_trace is not None
        self.assertEqual(detail.graph_trace["source"], "execution")
        self.assertEqual(detail.graph_trace["command"]["operation"], "generate")
        self.assertEqual(detail.graph_trace["plan"]["step_count"], 2)
        workers = {item["step_id"]: item for item in detail.graph_trace["workers"]}
        self.assertEqual(workers["slides_generate"]["elapsed_ms"], 28.0)
        self.assertEqual(workers["slides_generate"]["artifact_count"], 1)
        self.assertEqual(workers["doc_generate"]["reply_preview"], "doc ready")
        plan_steps = {item["step_id"]: item for item in detail.graph_trace["plan"]["steps"]}
        self.assertEqual(plan_steps["doc_generate"]["agent"], "DocAgent")
        self.assertEqual(plan_steps["doc_generate"]["goal"], "doc goal")
        self.assertEqual(plan_steps["slides_generate"]["status"], "done")
        self.assertEqual(detail.graph_trace["comparison"]["status"], "match")
        self.assertEqual(detail.graph_trace["comparison"]["graph_route"], "doc")
        self.assertEqual(detail.graph_trace["comparison"]["confidence_delta"], 0.01)
        self.assertEqual(
            [item["agent"] for item in detail.graph_trace["review"]["checks"]],
            ["ValidatorAgent", "ShieldAgent"],
        )
        self.assertEqual(detail.graph_trace["review"]["checks"][0]["status"], "passed")
        self.assertEqual(detail.graph_trace["reply_preview"], "all ready")

    def test_resolve_confirmation_updates_run_status(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="生成汇报稿",
            source_type="p2p",
        )
        self.service.update_task_run(created.task_run_id, status="waiting_confirmation", stage="awaiting_user_confirmation")
        confirmation = self.service.create_confirmation(
            created.task_run_id,
            prompt="是否继续？",
            options=["继续", "取消"],
        )

        resolved = self.service.resolve_confirmation(
            created.task_run_id,
            confirmation_id=confirmation.confirmation_id,
            answer_value="继续",
            answered_by="tester",
        )
        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.status, "answered")
        self.assertEqual(resolved.answer_value, "继续")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.status, "running")
        self.assertEqual(detail.stage, "confirmation_resolved")
        self.assertEqual(detail.confirmations[0].answered_by, "tester")

    def test_resolve_confirmation_does_not_overwrite_answered_confirmation(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="生成汇报稿",
            source_type="p2p",
        )
        self.service.update_task_run(created.task_run_id, status="waiting_confirmation", stage="awaiting_user_confirmation")
        confirmation = self.service.create_confirmation(
            created.task_run_id,
            prompt="是否继续？",
            options=["继续", "取消"],
        )

        first = self.service.resolve_confirmation(
            created.task_run_id,
            confirmation_id=confirmation.confirmation_id,
            answer_value="继续",
            answered_by="tester",
        )
        second = self.service.resolve_confirmation(
            created.task_run_id,
            confirmation_id=confirmation.confirmation_id,
            answer_value="取消",
            answered_by="other",
        )
        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertTrue(second.already_answered)
        self.assertEqual(second.answer_value, "继续")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.confirmations[0].answer_value, "继续")
        self.assertEqual(detail.confirmations[0].answered_by, "tester")

    def test_update_task_run_accepts_partial_failed_as_terminal_status(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="生成多产物",
            source_type="group",
        )
        self.service.update_task_run(created.task_run_id, status="running", stage="executing_artifacts")

        updated = self.service.update_task_run(
            created.task_run_id,
            status="partial_failed",
            stage="delivered_with_partial_failure",
            latest_error="slides failed",
        )
        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(updated)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.status, "partial_failed")
        self.assertEqual(detail.stage, "delivered_with_partial_failure")
        self.assertEqual(detail.latest_error, "slides failed")
        self.assertIsNotNone(detail.completed_at)

    def test_update_task_run_rejects_terminal_to_running_transition(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="已完成任务",
            source_type="group",
        )
        self.service.update_task_run(created.task_run_id, status="running", stage="processing")
        self.service.update_task_run(created.task_run_id, status="completed", stage="delivered")

        with self.assertLogs("app.services.task_run_service", level="WARNING") as logs:
            updated = self.service.update_task_run(created.task_run_id, status="running", stage="late_resume")
        detail = self.service.get_task_run(created.task_run_id)

        self.assertIsNotNone(updated)
        self.assertTrue(any("Rejected task run status transition" in line for line in logs.output))
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.status, "completed")
        self.assertEqual(detail.stage, "late_resume")
        self.assertIsNotNone(detail.completed_at)

    def test_update_task_run_rejects_unknown_status(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_test",
            title="未知状态",
            source_type="group",
        )

        with self.assertLogs("app.services.task_run_service", level="WARNING") as logs:
            self.service.update_task_run(created.task_run_id, status="mystery", stage="processing")
        detail = self.service.get_task_run(created.task_run_id)

        self.assertTrue(any("unknown task run status" in line for line in logs.output))
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.status, "queued")
        self.assertEqual(detail.stage, "processing")

    def test_create_run_publishes_realtime_events(self) -> None:
        with patch("app.services.task_run_service.realtime_hub.emit_room") as emit_room:
            created = self.service.create_task_run(
                session_id="oc_demo",
                title="创建协作任务",
                source_type="group",
            )

        self.assertTrue(created.task_run_id.startswith("run_"))
        self.assertEqual(emit_room.call_count, 3)
        first_room, first_message = emit_room.call_args_list[0].args
        second_room, second_message = emit_room.call_args_list[1].args
        third_room, third_message = emit_room.call_args_list[2].args
        self.assertEqual(first_room, f"task-run:{created.task_run_id}")
        self.assertEqual(second_room, "session:oc_demo")
        self.assertEqual(third_room, "task-runs:all")
        self.assertEqual(first_message["type"], "task_run.created")
        self.assertEqual(first_message["task_run"]["task_run_id"], created.task_run_id)
        self.assertEqual(second_message["task_run"]["session_id"], "oc_demo")
        self.assertEqual(third_message["task_run"]["session_id"], "oc_demo")
        self.assertEqual(second_message["task_run"]["session_label"], "产品讨论群")
        self.assertEqual(third_message["task_run"]["session_label"], "产品讨论群")

    def test_summary_payload_exposes_lifecycle_metadata(self) -> None:
        with patch("app.services.task_run_service.realtime_hub.emit_room") as emit_room:
            created = self.service.create_task_run(
                session_id="oc_demo",
                title="生成需求方案文档",
                source_type="group",
                metadata={
                    "run_kind": "artifact_lifecycle",
                    "primary_object": "doc",
                    "lifecycle_stage": "document",
                },
            )

        self.assertEqual(created.run_kind, "artifact_lifecycle")
        self.assertEqual(created.primary_object, "doc")
        self.assertEqual(created.lifecycle_stage, "document")
        first_message = emit_room.call_args_list[0].args[1]
        self.assertEqual(first_message["task_run"]["run_kind"], "artifact_lifecycle")
        self.assertEqual(first_message["task_run"]["primary_object"], "doc")
        self.assertEqual(first_message["task_run"]["lifecycle_stage"], "document")

    def test_get_task_run_tolerates_session_document_failure(self) -> None:
        service = TaskRunService(
            session_display_service=self.session_display_service,
            session_document_service=_StubSessionDocumentService(error=RuntimeError("doc history unavailable")),
        )
        created = service.create_task_run(
            session_id="oc_demo",
            title="创建协作任务",
            source_type="group",
        )

        detail = service.get_task_run(created.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.task_run_id, created.task_run_id)
        self.assertEqual(detail.session_documents, [])

    def test_get_task_run_tolerates_invalid_session_document_timestamp(self) -> None:
        service = TaskRunService(
            session_display_service=self.session_display_service,
            session_document_service=_StubSessionDocumentService(
                documents=[
                    {
                        "session_id": "oc_demo",
                        "document_id": "doc_1",
                        "title": "需求文档",
                        "version": "draft",
                        "sync_mode": "created",
                        "updated_at": "not-a-date",
                        "is_current": True,
                    }
                ]
            ),
        )
        created = service.create_task_run(
            session_id="oc_demo",
            title="创建协作任务",
            source_type="group",
        )

        detail = service.get_task_run(created.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(len(detail.session_documents), 1)
        self.assertIsNone(detail.session_documents[0].updated_at)
        self.assertEqual(detail.session_documents[0].version, 1)

    def test_detail_dedupes_session_document_and_document_artifact_views(self) -> None:
        service = TaskRunService(
            session_display_service=self.session_display_service,
            session_document_service=_StubSessionDocumentService(
                documents=[
                    {
                        "session_id": "oc_demo",
                        "document_id": "doc_1",
                        "title": "闇€姹傛枃妗?",
                        "url": "https://feishu.cn/docx/doc_1",
                        "version": 1,
                        "sync_mode": "created",
                        "is_current": True,
                    }
                ]
            ),
        )
        created = service.create_task_run(
            session_id="oc_demo",
            title="鍒涘缓鍗忎綔浠诲姟",
            source_type="group",
        )
        service.create_artifact(
            created.task_run_id,
            artifact_type="document",
            title="闇€姹傛枃妗?",
            provider="feishu_doc",
            url="https://feishu.cn/docx/doc_1",
            preview={
                "sync": {
                    "document_id": "doc_1",
                    "title": "闇€姹傛枃妗?",
                    "url": "https://feishu.cn/docx/doc_1",
                    "version": 1,
                }
            },
        )

        detail = service.get_task_run(created.task_run_id)

        self.assertIsNotNone(detail)
        assert detail is not None
        document_sources = [item for item in detail.context_pack.used_sources if item.kind == "document"]
        self.assertEqual(len(document_sources), 1)
        checks = {item.key: item.detail for item in detail.artifact_checks}
        self.assertIn("1", checks["document"])
        self.assertIn("1", checks["shareable_links"])
        self.assertIn("1", checks["delivery_bundle"])

    def test_list_task_runs_can_filter_by_session_label_query(self) -> None:
        self.service.create_task_run(
            session_id="oc_alpha",
            title="群聊任务一",
            source_type="group",
        )
        self.service.create_task_run(
            session_id="oc_beta",
            title="单聊任务二",
            source_type="p2p",
        )

        results = self.service.list_task_runs(session_query="产品讨论", limit=20)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].session_id, "oc_alpha")

    def test_upsert_step_is_atomic_under_concurrency(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_parallel_step",
            title="parallel step test",
            source_type="group",
        )
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(status: str, output: str) -> None:
            try:
                barrier.wait(timeout=5)
                self.service.upsert_step(
                    created.task_run_id,
                    step_key="request_received",
                    title="receive request",
                    status=status,
                    output_payload={"value": output},
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("running", "a")),
            threading.Thread(target=worker, args=("done", "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        with self.test_session_local() as session:
            rows = session.query(TaskRunStep).filter(
                TaskRunStep.task_run_id == created.task_run_id,
                TaskRunStep.step_key == "request_received",
            ).all()
        self.assertEqual(len(rows), 1)

    def test_merge_task_run_metadata_is_atomic_under_concurrency(self) -> None:
        created = self.service.create_task_run(
            session_id="oc_parallel_meta",
            title="parallel metadata test",
            source_type="group",
            metadata={"seed": True},
        )
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(patch_data: dict) -> None:
            try:
                barrier.wait(timeout=5)
                self.service.merge_task_run_metadata(created.task_run_id, patch_data)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=({"left": 1},)),
            threading.Thread(target=worker, args=({"right": 2},)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        metadata = self.service.get_task_run_metadata(created.task_run_id)
        self.assertTrue(metadata["seed"])
        self.assertEqual(metadata["left"], 1)
        self.assertEqual(metadata["right"], 2)


if __name__ == "__main__":
    unittest.main()
