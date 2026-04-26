import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Artifact, ConfirmationRequest, TaskRun, TaskRunStep
from app.services.task_run_service import TaskRunService


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

        self.service = TaskRunService()

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
        self.assertEqual(len(detail.steps), 1)
        self.assertEqual(detail.steps[0].step_key, "request_received")
        self.assertEqual(len(detail.artifacts), 1)
        self.assertEqual(detail.artifacts[0].artifact_type, "document")
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
        self.assertEqual(emit_room.call_count, 2)
        first_room, first_message = emit_room.call_args_list[0].args
        second_room, second_message = emit_room.call_args_list[1].args
        self.assertEqual(first_room, f"task-run:{created.task_run_id}")
        self.assertEqual(second_room, "session:oc_demo")
        self.assertEqual(first_message["type"], "task_run.created")
        self.assertEqual(first_message["task_run"]["task_run_id"], created.task_run_id)
        self.assertEqual(second_message["task_run"]["session_id"], "oc_demo")


if __name__ == "__main__":
    unittest.main()
