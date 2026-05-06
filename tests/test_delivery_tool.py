import unittest

from app.services.delivery_artifact_service import DeliveryArtifactService
from app.services.tools.delivery_tool import DeliveryTool


class FakeTaskRunService:
    def __init__(self) -> None:
        self.steps = []

    def upsert_step(self, *args, **kwargs):
        self.steps.append({"args": args, "kwargs": kwargs})


class DeliveryToolTests(unittest.TestCase):
    def test_record_feishu_sync_step_maps_ready_status(self) -> None:
        task_run_service = FakeTaskRunService()
        tool = DeliveryTool(
            delivery_artifact_service=DeliveryArtifactService(),
            task_run_service=task_run_service,
        )

        tool._record_feishu_sync_step(
            "run_1",
            {
                "status": "ready",
                "sync_mode": "delivery_created",
                "document_id": "doc_1",
                "url": "https://feishu.example/doc_1",
            },
            requested_by="tester",
        )

        self.assertEqual(task_run_service.steps[0]["kwargs"]["step_key"], "artifact_feishu_sync")
        self.assertEqual(task_run_service.steps[0]["kwargs"]["status"], "done")
        self.assertEqual(task_run_service.steps[0]["kwargs"]["input_payload"]["trigger"], "delivery_bundle")

    def test_record_feishu_sync_step_maps_failed_status_and_error(self) -> None:
        task_run_service = FakeTaskRunService()
        tool = DeliveryTool(
            delivery_artifact_service=DeliveryArtifactService(),
            task_run_service=task_run_service,
        )

        tool._record_feishu_sync_step(
            "run_1",
            {
                "status": "failed",
                "sync_mode": "failed",
                "warnings": ["upload failed"],
            },
            requested_by="tester",
        )

        self.assertEqual(task_run_service.steps[0]["kwargs"]["status"], "failed")
        self.assertEqual(task_run_service.steps[0]["kwargs"]["error"], "upload failed")

    def test_attach_feishu_sync_to_manifest_marks_each_synced_deliverable(self) -> None:
        tool = DeliveryTool(
            delivery_artifact_service=DeliveryArtifactService(),
            task_run_service=FakeTaskRunService(),
        )
        manifest = {
            "deliverables": [
                {"key": "slides", "artifact_type": "slides_package", "title": "PPT"},
                {"key": "canvas", "artifact_type": "canvas", "title": "Canvas"},
            ],
            "artifact_summaries": [
                {"key": "slides", "artifact_type": "slides_package", "title": "PPT"},
                {"key": "canvas", "artifact_type": "canvas", "title": "Canvas"},
            ],
            "artifacts": [
                {"artifact_type": "slides_package", "title": "PPT"},
                {"artifact_type": "canvas", "title": "Canvas"},
            ],
        }
        feishu_delivery = {
            "status": "ready",
            "document_id": "doc_1",
            "document_url": "https://feishu.example/doc_1",
            "sync_mode": "delivery_created",
            "synced_at": "2026-05-06T00:00:00+00:00",
            "items": [
                {"kind": "slides", "source_url": "https://public.example/slides.html"},
                {"kind": "canvas", "source_url": "https://public.example/canvas.html"},
            ],
            "artifact_syncs": {
                "slides": {
                    "kind": "slides",
                    "status": "ready",
                    "document_id": "doc_1",
                    "document_url": "https://feishu.example/doc_1",
                    "source_url": "https://public.example/slides.html",
                    "items": [
                        {"kind": "slides", "source_url": "https://public.example/slides.html"},
                        {"kind": "slides_pptx", "status": "ready", "file_token": "pptx_token"},
                    ],
                    "media_items": [{"kind": "slides_pptx", "status": "ready", "file_token": "pptx_token"}],
                    "warnings": [],
                }
            },
            "media_items": [
                {
                    "kind": "slides_pptx",
                    "status": "ready",
                    "file_token": "pptx_token",
                    "source_url": "https://public.example/slides.pptx",
                },
                {
                    "kind": "canvas_image",
                    "status": "ready",
                    "file_token": "canvas_token",
                    "source_url": "https://public.example/canvas.svg",
                },
            ],
            "warnings": [],
        }

        tool._attach_feishu_sync_to_manifest(manifest, feishu_delivery)

        self.assertEqual(manifest["deliverables"][0]["feishu_sync"]["document_id"], "doc_1")
        self.assertEqual(manifest["deliverables"][0]["feishu_sync"]["status"], "ready")
        self.assertEqual(manifest["deliverables"][0]["feishu_sync"]["media_items"][0]["kind"], "slides_pptx")
        self.assertEqual(manifest["deliverables"][0]["feishu_sync"]["items"][1]["file_token"], "pptx_token")
        self.assertEqual(manifest["deliverables"][1]["feishu_sync"]["kind"], "canvas")
        self.assertEqual(manifest["deliverables"][1]["feishu_sync"]["media_items"][0]["file_token"], "canvas_token")
        self.assertEqual(manifest["artifacts"][0]["feishu_sync"]["kind"], "slides")
        self.assertEqual(manifest["artifacts"][1]["feishu_sync"]["source_url"], "https://public.example/canvas.html")


if __name__ == "__main__":
    unittest.main()
