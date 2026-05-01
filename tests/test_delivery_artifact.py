import json
import tempfile
import unittest
from pathlib import Path

from app.services.delivery_artifact_service import DeliveryArtifactService


class DeliveryArtifactServiceTests(unittest.TestCase):
    def test_delivery_service_persists_json_and_html(self) -> None:
        manifest = {
            "title": "任务交付包",
            "task_run_id": "run_123",
            "session_id": "s1",
            "summary": "本次任务已整理 2 个交付物。",
            "checks": [
                {"key": "im_entry", "label": "IM 入口", "status": "ready", "detail": "m1"},
                {"key": "document", "label": "文档产物", "status": "ready", "detail": "已生成"},
            ],
            "artifacts": [
                {
                    "artifact_id": "artifact_doc",
                    "artifact_type": "document",
                    "title": "需求文档",
                    "url": "/api/artifacts/doc/run_123.md",
                    "version": 1,
                }
            ],
            "next_steps": ["贴回 IM 会话。"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            service = DeliveryArtifactService(root_dir=Path(tmpdir))

            artifact = service.persist_bundle(manifest, task_run_id="run_123", session_id="s1")

            self.assertEqual(artifact["artifact_type"], "delivery_bundle")
            self.assertEqual(artifact["url"], "/api/artifacts/delivery/run_123.html")
            self.assertTrue((Path(tmpdir) / "run_123.html").is_file())
            json_path = Path(tmpdir) / "run_123.json"
            self.assertTrue(json_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "agent-pilot.delivery.v1")
            self.assertEqual(payload["exports"]["html"], "/api/artifacts/delivery/run_123.html")

    def test_delivery_service_tolerates_non_numeric_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = DeliveryArtifactService(root_dir=Path(tmpdir))

            artifact = service.persist_bundle(
                {"title": "任务交付包", "version": "draft"},
                task_run_id="run_123",
                session_id="s1",
            )

            self.assertEqual(artifact["version"], 1)
            self.assertEqual(artifact["preview"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
