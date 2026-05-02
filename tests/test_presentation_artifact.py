import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services.presentation_artifact_service import PresentationArtifactService
from app.services.tools.presentation_tool import PresentationTool


class PresentationArtifactServiceTests(unittest.TestCase):
    def test_presentation_service_persists_json_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = PresentationArtifactService(root_dir=Path(tmpdir))
            artifact = service.persist_package(
                {
                    "theme": "报名汇报",
                    "audience": "评委演示",
                    "slides": [
                        {
                            "title": "背景",
                            "bullets": ["IM 讨论分散", "需要自动沉淀"],
                        }
                    ],
                },
                provider="llm",
                task_run_id="run_slides",
                session_id="s1",
            )

            json_path = Path(tmpdir) / "run_slides.json"
            html_path = Path(tmpdir) / "run_slides.html"
            pptx_path = Path(tmpdir) / "run_slides.pptx"

            self.assertEqual(artifact["artifact_type"], "slides_package")
            self.assertEqual(artifact["provider"], "llm")
            self.assertEqual(artifact["url"], "/api/artifacts/slides/run_slides.html")
            self.assertEqual(artifact["preview"]["exports"]["json"], "/api/artifacts/slides/run_slides.json")
            self.assertEqual(artifact["preview"]["exports"]["html"], "/api/artifacts/slides/run_slides.html")
            self.assertEqual(artifact["preview"]["exports"]["pptx"], "/api/artifacts/slides/run_slides.pptx")
            self.assertTrue(json_path.is_file())
            self.assertTrue(html_path.is_file())
            self.assertTrue(pptx_path.is_file())
            self.assertEqual(artifact["preview"]["slides"][0]["duration_sec"], 45)
            self.assertIn("speaker_notes", artifact["preview"]["slides"][0])
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("报名汇报", html)
            self.assertIn("Speaker notes", html)
            self.assertNotIn("鎶", html)
            self.assertNotIn("Speaker notes/strong", html)
            with zipfile.ZipFile(pptx_path) as archive:
                self.assertIn("ppt/presentation.xml", archive.namelist())
                self.assertIn("ppt/slides/slide1.xml", archive.namelist())
            from pptx import Presentation

            deck = Presentation(pptx_path)
            self.assertEqual(len(deck.slides), 1)

            reply = PresentationTool(artifact_service=service).format_reply(artifact["preview"], artifact=artifact)
            self.assertIn("产物链接：", reply)
            self.assertIn("预览链接：", reply)
            self.assertIn("PPT 下载：", reply)
            self.assertIn("run_slides.pptx", reply)

    def test_presentation_service_tolerates_non_numeric_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = PresentationArtifactService(root_dir=Path(tmpdir))
            artifact = service.persist_package(
                {"theme": "报名汇报", "version": "draft", "slides": []},
                provider="llm",
                task_run_id="run_slides",
                session_id="s1",
            )

            self.assertEqual(artifact["version"], 1)
            self.assertEqual(artifact["preview"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
