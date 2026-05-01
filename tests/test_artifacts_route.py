import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.api.routes.artifacts import (
    get_local_canvas_artifact,
    get_local_delivery_artifact,
    get_local_doc_artifact,
    get_local_slides_artifact,
)


class ArtifactRouteTests(unittest.TestCase):
    def test_get_local_doc_artifact_returns_markdown_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "doc"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.md"
                path.write_text("# Sample\n", encoding="utf-8")

                response = asyncio.run(get_local_doc_artifact("sample.md"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.md")
        self.assertEqual(response.media_type, "text/markdown; charset=utf-8")

    def test_get_local_doc_artifact_rejects_traversal(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(get_local_doc_artifact("../secret.md"))

        self.assertEqual(context.exception.status_code, 404)

    def test_get_local_canvas_artifact_returns_json_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "canvas"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.json"
                path.write_text('{"shapes": []}', encoding="utf-8")

                response = asyncio.run(get_local_canvas_artifact("sample.json"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.json")
        self.assertEqual(response.media_type, "application/json")

    def test_get_local_canvas_artifact_returns_svg_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "canvas"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.svg"
                path.write_text("<svg></svg>", encoding="utf-8")

                response = asyncio.run(get_local_canvas_artifact("sample.svg"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.svg")
        self.assertEqual(response.media_type, "image/svg+xml")

    def test_get_local_canvas_artifact_returns_html_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "canvas"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.html"
                path.write_text("<html></html>", encoding="utf-8")

                response = asyncio.run(get_local_canvas_artifact("sample.html"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.html")
        self.assertEqual(response.media_type, "text/html; charset=utf-8")

    def test_get_local_slides_artifact_returns_html_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "slides"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.html"
                path.write_text("<html></html>", encoding="utf-8")

                response = asyncio.run(get_local_slides_artifact("sample.html"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.html")
        self.assertEqual(response.media_type, "text/html; charset=utf-8")

    def test_get_local_slides_artifact_rejects_unsupported_extension(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "slides"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.txt"
                path.write_text("nope", encoding="utf-8")

                with self.assertRaises(HTTPException) as context:
                    asyncio.run(get_local_slides_artifact("sample.txt"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(context.exception.status_code, 404)

    def test_get_local_slides_artifact_returns_pptx_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "slides"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.pptx"
                path.write_bytes(b"pptx")

                response = asyncio.run(get_local_slides_artifact("sample.pptx"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.pptx")
        self.assertEqual(
            response.media_type,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

    def test_get_local_delivery_artifact_returns_html_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "delivery"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.html"
                path.write_text("<html></html>", encoding="utf-8")

                response = asyncio.run(get_local_delivery_artifact("sample.html"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.html")
        self.assertEqual(response.media_type, "text/html; charset=utf-8")

    def test_get_local_delivery_artifact_rejects_traversal(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(get_local_delivery_artifact("../secret.html"))

        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
