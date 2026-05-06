import unittest
from datetime import datetime, timezone

from app.schemas.task_run import (
    ArtifactRecord,
    ConfirmationRequestRecord,
    SessionDocumentRecord,
    TaskRunDetail,
)
from app.services.next_action_service import ContextualNextActionService
from app.services.response_formatter import ResponseFormatter
from app.services.workflow.status_execution import WorkflowStatusExecution


def _detail(
    *,
    status: str = "completed",
    intent: str = "doc",
    artifacts: list[ArtifactRecord] | None = None,
    confirmations: list[ConfirmationRequestRecord] | None = None,
    session_documents: list[SessionDocumentRecord] | None = None,
    latest_error: str | None = None,
    latest_reply_preview: str | None = None,
) -> TaskRunDetail:
    return TaskRunDetail(
        task_run_id="run_next",
        session_id="session_1",
        source_type="group",
        title="协作任务",
        stage="delivered",
        status=status,
        intent=intent,
        latest_error=latest_error,
        latest_reply_preview=latest_reply_preview,
        created_at=datetime.now(timezone.utc),
        artifacts=artifacts or [],
        confirmations=confirmations or [],
        session_documents=session_documents or [],
    )


def _artifact(artifact_type: str, *, provider: str = "local", url: str | None = "/artifact") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact_{artifact_type}",
        artifact_type=artifact_type,
        provider=provider,
        title=artifact_type,
        status="ready",
        url=url,
    )


def _document() -> SessionDocumentRecord:
    return SessionDocumentRecord(
        session_id="session_1",
        document_id="doc_1",
        title="项目文档",
        version=3,
        sync_mode="updated",
        is_current=True,
    )


class ContextualNextActionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ContextualNextActionService()

    def test_waiting_confirmation_recommends_answer_first(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                status="waiting_confirmation",
                confirmations=[
                    ConfirmationRequestRecord(
                        confirmation_id="confirm_1",
                        prompt="请选择目标文档",
                        options_json='["项目周报"]',
                        status="pending",
                    )
                ],
            )
        )

        self.assertEqual(bundle.recommendations[0].action_type, "answer_confirmation")
        self.assertEqual(bundle.recommendations[0].priority, "high")
        self.assertEqual(bundle.recommendations[0].target_id, "confirm_1")

    def test_doc_without_slides_recommends_slides_and_canvas(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[_artifact("document", provider="feishu_doc", url="https://feishu.cn/docx/doc_1")],
                session_documents=[_document()],
            )
        )
        action_types = [item.action_type for item in bundle.recommendations]

        self.assertIn("generate_slides", action_types)
        self.assertIn("generate_canvas", action_types)
        self.assertLessEqual(len(bundle.recommendations), 3)

    def test_complete_core_artifacts_can_recommend_bundle_delivery(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[
                    _artifact("document", provider="feishu_doc", url="https://feishu.cn/docx/doc_1"),
                    _artifact("slides_package"),
                ],
                session_documents=[_document()],
            )
        )
        action_types = [item.action_type for item in bundle.recommendations]

        self.assertIn("bundle_delivery", action_types)
        self.assertNotEqual(bundle.recommendations[0].action_type, "bundle_delivery")

    def test_canvas_without_slides_does_not_recommend_slides_revision(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[
                    _artifact("document", provider="feishu_doc", url="https://feishu.cn/docx/doc_1"),
                    _artifact("canvas"),
                ],
                session_documents=[_document()],
            )
        )
        action_types = [item.action_type for item in bundle.recommendations]

        self.assertIn("generate_slides", action_types)
        self.assertIn("bundle_delivery", action_types)
        self.assertNotIn("revise_slides", action_types)

    def test_slides_without_document_does_not_recommend_bundle_delivery(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[_artifact("slides_package")],
                session_documents=[],
            )
        )

        self.assertNotIn("bundle_delivery", [item.action_type for item in bundle.recommendations])

    def test_sync_failure_recommends_retry_and_avoids_share(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                status="failed",
                artifacts=[_artifact("document", provider="local", url=None)],
                latest_error="Document sync failed: create down",
            )
        )
        action_types = [item.action_type for item in bundle.recommendations]

        self.assertEqual(action_types[0], "retry_sync")
        self.assertNotIn("share_to_im", action_types)

    def test_zero_max_items_returns_no_recommendations(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[_artifact("document", provider="feishu_doc", url="https://feishu.cn/docx/doc_1")],
                session_documents=[_document()],
            ),
            max_items=0,
        )

        self.assertEqual(bundle.recommendations, [])

    def test_formatter_appends_next_action_block(self) -> None:
        bundle = self.service.build_for_task_run(
            _detail(
                artifacts=[_artifact("document", provider="feishu_doc", url="https://feishu.cn/docx/doc_1")],
                session_documents=[_document()],
            )
        )

        reply = ResponseFormatter().append_next_actions("【文档同步】\n已完成", bundle)

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("我建议下一步可以：", reply)
        self.assertIn("基于当前文档生成汇报 PPT", reply)

    def test_pending_requirement_discussion_recommends_document_first(self) -> None:
        source_text = "\n".join(
            [
                "我们这次想做校园活动报名与审核系统，主要解决报名信息分散的问题。",
                "目标用户主要是学生、社团负责人和学院老师。",
                "核心流程是查看活动、提交报名、负责人审核、生成名单。",
            ]
        )

        reply = WorkflowStatusExecution.format_lifecycle_discussion_next_action_reply(source_text)

        self.assertIn("整理成正式需求方案文档", reply)
        self.assertIn("生成汇报 PPT", reply)
        self.assertTrue(WorkflowStatusExecution.should_recommend_lifecycle_doc_from_discussion(source_text))


if __name__ == "__main__":
    unittest.main()
