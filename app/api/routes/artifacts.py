from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


router = APIRouter()


@router.get("/doc/{filename}")
async def get_local_doc_artifact(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path("data") / "artifacts" / "doc" / filename
    if not path.is_file() or path.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=filename)


@router.get("/canvas/{filename}")
async def get_local_canvas_artifact(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path("data") / "artifacts" / "canvas" / filename
    if not path.is_file() or path.suffix.lower() not in {".json", ".svg"}:
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else "application/json"
    return FileResponse(path, media_type=media_type, filename=filename)
