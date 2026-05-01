from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


router = APIRouter()

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


def _serve_local_artifact(kind: str, filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    media_types = ARTIFACT_MEDIA_TYPES[kind]
    path = Path("data") / "artifacts" / kind / filename
    suffix = path.suffix.lower()
    if not path.is_file() or suffix not in media_types:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    return FileResponse(path, media_type=media_types[suffix], filename=filename)


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
