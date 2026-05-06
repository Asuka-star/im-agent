import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy import desc, or_, select

from app.db.database import SessionLocal
from app.db.models import Artifact
from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.presentation_artifact_service import PresentationArtifactService


router = APIRouter()
logger = logging.getLogger(__name__)

ARTIFACT_NOT_FOUND = "Artifact not found"
ARTIFACT_MEDIA_TYPES = {
    "doc": {".md": "text/markdown; charset=utf-8"},
    "canvas": {
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    },
    "slides": {
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    },
    "delivery": {
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
    },
}


def _validate_artifact_filename(filename: str) -> None:
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)


def _serve_local_artifact(kind: str, filename: str) -> FileResponse | Response:
    _validate_artifact_filename(filename)
    media_types = ARTIFACT_MEDIA_TYPES[kind]
    path = Path("data") / "artifacts" / kind / filename
    suffix = path.suffix.lower()
    if suffix not in media_types:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    if not path.is_file():
        restored = _serve_artifact_from_db_preview(kind, filename, suffix)
        if restored is not None:
            return restored
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    return FileResponse(path, media_type=media_types[suffix], filename=filename)


def _serve_artifact_from_db_preview(kind: str, filename: str, suffix: str) -> Response | None:
    preview = _load_artifact_preview(kind, filename)
    if preview is None:
        return None
    if kind == "canvas":
        return _canvas_preview_response(preview, suffix)
    if kind == "slides":
        return _slides_preview_response(preview, suffix)
    return None


def _load_artifact_preview(kind: str, filename: str) -> dict | None:
    artifact_types = {
        "canvas": ("canvas",),
        "slides": ("slides_package", "slides"),
    }.get(kind)
    if not artifact_types:
        return None
    relative_url = f"/api/artifacts/{kind}/{filename}"
    try:
        with SessionLocal() as session:
            rows = session.execute(
                select(Artifact)
                .where(
                    Artifact.artifact_type.in_(artifact_types),
                    or_(
                        Artifact.url == relative_url,
                        Artifact.url.like(f"%/{filename}"),
                        Artifact.preview_json.like(f"%{filename}%"),
                    ),
                )
                .order_by(desc(Artifact.id))
                .limit(20)
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to load artifact preview fallback: kind=%s filename=%s error=%s", kind, filename, exc)
        return None

    for row in rows:
        preview = _decode_preview(row.preview_json)
        if preview and _artifact_row_matches(row, preview, relative_url, filename):
            return preview
    return None


def _decode_preview(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _artifact_row_matches(row: Artifact, preview: dict, relative_url: str, filename: str) -> bool:
    row_url = str(row.url or "").strip()
    if row_url == relative_url or row_url.endswith(f"/{filename}"):
        return True
    exports = preview.get("exports")
    if isinstance(exports, dict):
        return any(
            str(value or "").strip() == relative_url
            or str(value or "").strip().endswith(f"/{filename}")
            for value in exports.values()
        )
    return False


def _canvas_preview_response(preview: dict, suffix: str) -> Response | None:
    service = CanvasArtifactService()
    if suffix == ".json":
        return JSONResponse(preview)
    if suffix == ".svg":
        return Response(service._build_svg(preview), media_type="image/svg+xml")
    if suffix == ".html":
        svg = service._build_svg(preview)
        return HTMLResponse(service._build_html(preview, svg))
    return None


def _slides_preview_response(preview: dict, suffix: str) -> Response | None:
    service = PresentationArtifactService()
    if suffix == ".json":
        return JSONResponse(preview)
    if suffix == ".html":
        return HTMLResponse(service._render_html(preview))
    return None


@router.get("/doc/{filename}")
async def get_local_doc_artifact(filename: str) -> FileResponse:
    return _serve_local_artifact("doc", filename)


@router.get("/canvas/{filename}")
async def get_local_canvas_artifact(filename: str) -> FileResponse:
    return _serve_local_artifact("canvas", filename)


@router.get("/slides/{filename}")
async def get_local_slides_artifact(filename: str) -> FileResponse:
    return _serve_local_artifact("slides", filename)


@router.get("/delivery/{filename}")
async def get_local_delivery_artifact(filename: str) -> FileResponse:
    return _serve_local_artifact("delivery", filename)
