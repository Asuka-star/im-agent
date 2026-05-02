import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services.artifact_skills import ArtifactVerifier, DocSkill, SlidesSkill


class ArtifactSkillTests(unittest.TestCase):
    def test_doc_skill_filters_presentation_outline_sections(self) -> None:
        package = DocSkill().normalize(
            {
                "title": "Doc",
                "sections": [
                    {"heading": "\u6587\u6863\u8bf4\u660e", "paragraphs": ["slide metadata"]},
                    {"heading": "P1. Context", "paragraphs": ["slide-only"]},
                    {"heading": "Discussion Summary", "paragraphs": ["keep"]},
                ],
            }
        )

        self.assertEqual([section["heading"] for section in package["sections"]], ["Discussion Summary"])
        self.assertTrue(DocSkill().verify(package).ok)

    def test_slides_skill_adds_style_and_normalizes_duration(self) -> None:
        package = SlidesSkill().normalize(
            {
                "theme": "Launch",
                "version": "draft",
                "slides": [{"title": "Context", "bullets": ["a", "b", "c", "d", "e", "f"], "duration_sec": 999}],
            }
        )

        self.assertEqual(package["version"], 1)
        self.assertEqual(package["slides"][0]["duration_sec"], 180)
        self.assertEqual(len(package["slides"][0]["bullets"]), 5)
        self.assertIn("style", package)

    def test_verifier_rejects_invalid_pptx_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "")

            result = ArtifactVerifier().verify_pptx_file(path)

        self.assertFalse(result.ok)
        self.assertTrue(any("ppt/presentation.xml" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
