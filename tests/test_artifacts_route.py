import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.artifacts import (
    get_local_canvas_artifact,
    get_local_delivery_artifact,
    get_local_doc_artifact,
    get_local_slides_artifact,
)
from app.db.models import Artifact


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
        self.assertIn("attachment", response.headers["content-disposition"])

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

    def test_get_local_canvas_artifact_returns_png_file(self) -> None:
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                artifact_dir = Path("data") / "artifacts" / "canvas"
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / "sample.png"
                path.write_bytes(b"png")

                response = asyncio.run(get_local_canvas_artifact("sample.png"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(Path(response.path).name, "sample.png")
        self.assertEqual(response.media_type, "image/png")

    def test_get_local_canvas_artifact_restores_html_from_db_preview_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{Path(tmpdir) / 'artifacts.db'}", connect_args={"check_same_thread": False})
            session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Artifact.__table__.create(bind=engine)
            preview = {
                "title": "产品流程图",
                "version": 1,
                "schema": "im-agent.canvas.v1",
                "exports": {
                    "json": "/api/artifacts/canvas/missing.json",
                    "svg": "/api/artifacts/canvas/missing.svg",
                    "html": "/api/artifacts/canvas/missing.html",
                },
                "shapes": [
                    {
                        "id": "n1",
                        "type": "node",
                        "text": "学生查看活动列表",
                        "x": 80,
                        "y": 140,
                        "w": 184,
                        "h": 72,
                        "color": "#EAF5FF",
                        "stroke": "#5A9FD6",
                        "group": "Input",
                    }
                ],
            }
            with session_local() as session:
                session.add(
                    Artifact(
                        artifact_id="artifact_canvas",
                        task_run_id="run_canvas",
                        artifact_type="canvas",
                        provider="local",
                        title="产品流程图",
                        status="ready",
                        url="/api/artifacts/canvas/missing.html",
                        version=1,
                        preview_json=json.dumps(preview, ensure_ascii=False),
                    )
                )
                session.commit()

            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                with patch("app.api.routes.artifacts.SessionLocal", session_local):
                    response = asyncio.run(get_local_canvas_artifact("missing.html"))
            finally:
                os.chdir(old_cwd)
                engine.dispose()

        self.assertEqual(response.media_type, "text/html")
        self.assertIn("产品流程图", response.body.decode("utf-8"))
        self.assertIn("学生查看活动列表", response.body.decode("utf-8"))

    def test_get_local_canvas_artifact_restores_svg_from_db_preview_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{Path(tmpdir) / 'artifacts.db'}", connect_args={"check_same_thread": False})
            session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Artifact.__table__.create(bind=engine)
            preview = {
                "title": "产品流程图",
                "version": 1,
                "schema": "im-agent.canvas.v1",
                "exports": {"svg": "/api/artifacts/canvas/missing.svg"},
                "shapes": [{"id": "n1", "type": "node", "text": "学生查看活动列表"}],
            }
            with session_local() as session:
                session.add(
                    Artifact(
                        artifact_id="artifact_canvas_svg",
                        task_run_id="run_canvas",
                        artifact_type="canvas",
                        provider="local",
                        title="产品流程图",
                        status="ready",
                        url="/api/artifacts/canvas/missing.html",
                        version=1,
                        preview_json=json.dumps(preview, ensure_ascii=False),
                    )
                )
                session.commit()

            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                with patch("app.api.routes.artifacts.SessionLocal", session_local):
                    response = asyncio.run(get_local_canvas_artifact("missing.svg"))
            finally:
                os.chdir(old_cwd)
                engine.dispose()

        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("<svg", response.body.decode("utf-8"))
        self.assertIn("学生查看活动列表", response.body.decode("utf-8"))

    def test_get_local_canvas_artifact_restores_png_from_db_preview_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_engine(f"sqlite:///{Path(tmpdir) / 'artifacts.db'}", connect_args={"check_same_thread": False})
            session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Artifact.__table__.create(bind=engine)
            preview = {
                "title": "产品流程图",
                "version": 1,
                "schema": "im-agent.canvas.v1",
                "exports": {"png": "/api/artifacts/canvas/missing.png"},
                "shapes": [{"id": "n1", "type": "node", "text": "学生查看活动列表"}],
            }
            with session_local() as session:
                session.add(
                    Artifact(
                        artifact_id="artifact_canvas_png",
                        task_run_id="run_canvas",
                        artifact_type="canvas",
                        provider="local",
                        title="产品流程图",
                        status="ready",
                        url="/api/artifacts/canvas/missing.html",
                        version=1,
                        preview_json=json.dumps(preview, ensure_ascii=False),
                    )
                )
                session.commit()

            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                with patch("app.api.routes.artifacts.SessionLocal", session_local):
                    response = asyncio.run(get_local_canvas_artifact("missing.png"))
            finally:
                os.chdir(old_cwd)
                engine.dispose()

        self.assertEqual(response.media_type, "image/png")
        self.assertTrue(response.body.startswith(b"\x89PNG\r\n\x1a\n"))

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
