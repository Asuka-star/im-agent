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
            "artifact_summaries": [
                {
                    "artifact_id": "artifact_slides",
                    "artifact_type": "slides_package",
                    "label": "演示稿",
                    "title": "评审演示稿",
                    "status": "ready",
                    "metrics": ["6 页", "讲者备注 6/6 页"],
                    "highlights": ["支持 HTML 预览和 PPTX 导出。"],
                    "warnings": [],
                    "exports": [{"label": "HTML", "url": "/api/artifacts/slides/run_123.html"}],
                }
            ],
            "highlights": ["验收清单：8 项已满足。"],
            "context_pack": {
                "summary": "已汇总 2 项可追溯材料，1 项信息建议补充。",
                "used_sources": [
                    {"kind": "im", "label": "飞书 IM 会话", "detail": "m1", "status": "ready"},
                ],
                "missing_items": [
                    {"kind": "canvas", "label": "白板 / Canvas", "detail": "尚未生成", "status": "missing"},
                ],
                "suggested_inputs": ["补充流程阶段和风险。"],
            },
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
            self.assertEqual(payload["artifact_summaries"][0]["label"], "演示稿")
            self.assertEqual(payload["context_pack"]["missing_items"][0]["kind"], "canvas")
            html_text = (Path(tmpdir) / "run_123.html").read_text(encoding="utf-8")
            self.assertIn("交付摘要", html_text)
            self.assertIn("上下文依据", html_text)
            self.assertIn("产物详情", html_text)
            self.assertIn("讲者备注 6/6 页", html_text)

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
