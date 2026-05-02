import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Artifact, ConfirmationRequest, TaskRun, TaskRunStep
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
            metadata={"chat_id": "chat_123"},
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


if __name__ == "__main__":
    unittest.main()
