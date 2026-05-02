import json
import unittest
from datetime import datetime, timezone

from app.schemas.task_run import ArtifactRecord, SessionDocumentRecord, TaskRunDetail, TaskRunStepRecord
from app.services.task_artifact_verifier import TaskArtifactVerifier


def _detail(*, artifacts: list[ArtifactRecord], session_documents: list[SessionDocumentRecord] | None = None) -> TaskRunDetail:
    return TaskRunDetail(
        task_run_id="run_checks",
        session_id="session_1",
        source_type="group",
        source_ref="chat_1",
        trigger_message_id="msg_1",
        title="场景 C/D 验收",
        stage="delivered",
        status="completed",
        created_at=datetime.now(timezone.utc),
        steps=[
            TaskRunStepRecord(
                step_key="request_received",
                title="接收用户请求",
                step_type="workflow",
                status="done",
            )
        ],
        artifacts=artifacts,
        session_documents=session_documents or [],
    )


def _artifact(artifact_type: str, *, preview: dict | None = None, url: str | None = "/artifact") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact_{artifact_type}",
        artifact_type=artifact_type,
        provider="local",
        title=artifact_type,
        status="ready",
        url=url,
        preview_json=json.dumps(preview, ensure_ascii=False) if preview is not None else None,
    )


class TaskArtifactVerifierTests(unittest.TestCase):
    def test_builds_scene_c_and_d_checks_from_artifacts(self) -> None:
        checks = TaskArtifactVerifier().build_for_task_run(
            _detail(
                artifacts=[
                    _artifact(
                        "canvas",
                        preview={
                            "schema": "im-agent.canvas.v1",
                            "shapes": [{"id": "n1", "type": "node"}],
                            "exports": {
                                "json": "/api/artifacts/canvas/run.json",
                                "svg": "/api/artifacts/canvas/run.svg",
                                "html": "/api/artifacts/canvas/run.html",
                            },
                        },
                    ),
                    _artifact(
                        "slides_package",
                        preview={
                            "slides": [
                                {
                                    "title": "开场",
                                    "bullets": ["价值"],
                                    "speaker_notes": "30 秒说明问题背景。",
                                    "duration_sec": 30,
                                }
                            ],
                            "exports": {
                                "html": "/api/artifacts/slides/run.html",
                                "pptx": "/api/artifacts/slides/run.pptx",
                            },
                        },
                    ),
                ],
                session_documents=[
                    SessionDocumentRecord(
                        session_id="session_1",
                        document_id="doc_1",
                        title="项目文档",
                        version=1,
                        sync_mode="created",
                        is_current=True,
                        url="https://feishu.cn/docx/doc_1",
                    )
                ],
            )
        )

        status_by_key = {item["key"]: item["status"] for item in checks}
        self.assertEqual(status_by_key["document"], "ready")
        self.assertEqual(status_by_key["canvas"], "ready")
        self.assertEqual(status_by_key["slides"], "ready")
        self.assertEqual(status_by_key["rehearsal"], "ready")
        self.assertEqual(status_by_key["delivery_bundle"], "partial")

    def test_marks_incomplete_exports_as_partial(self) -> None:
        checks = TaskArtifactVerifier().build_for_task_run(
            _detail(
                artifacts=[
                    _artifact(
                        "slides_package",
                        preview={"slides": [{"title": "开场"}], "exports": {"html": "/slides.html"}},
                    )
                ]
            )
        )

        by_key = {item["key"]: item for item in checks}
        self.assertEqual(by_key["slides"]["status"], "partial")
        self.assertIn("pptx", by_key["slides"]["detail"])
        self.assertEqual(by_key["rehearsal"]["status"], "missing")

    def test_session_document_links_count_as_shareable_delivery_items(self) -> None:
        checks = TaskArtifactVerifier().build_for_task_run(
            _detail(
                artifacts=[],
                session_documents=[
                    SessionDocumentRecord(
                        session_id="session_1",
                        document_id="doc_1",
                        title="飞书文档",
                        version=1,
                        sync_mode="created",
                        is_current=True,
                        url="https://feishu.cn/docx/doc_1",
                    )
                ],
            )
        )

        by_key = {item["key"]: item for item in checks}
        self.assertEqual(by_key["document"]["status"], "ready")
        self.assertEqual(by_key["shareable_links"]["status"], "ready")
        self.assertEqual(by_key["delivery_bundle"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
