from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuMediaAPI:
    """Uploads media assets for Feishu Docs/Docx blocks."""

    UPLOAD_PATH = "/open-apis/drive/v1/medias/upload_all"
    MAX_UPLOAD_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        *,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.auth_service = auth_service or FeishuAuthService()
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)

    def upload_docx_image(
        self,
        *,
        document_id: str,
        file_path: str | Path,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        return self.upload_media(
            file_path=file_path,
            file_name=file_name,
            parent_type="docx_image",
            parent_node=document_id,
        )

    def upload_docx_file(
        self,
        *,
        document_id: str,
        file_path: str | Path,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        return self.upload_media(
            file_path=file_path,
            file_name=file_name,
            parent_type="docx_file",
            parent_node=document_id,
        )

    def upload_import_file(
        self,
        *,
        file_path: str | Path,
        file_name: str | None = None,
        file_extension: str,
        obj_type: str,
    ) -> dict[str, Any]:
        return self.upload_media(
            file_path=file_path,
            file_name=file_name,
            parent_type="ccm_import_open",
            parent_node="",
            extra={
                "obj_type": str(obj_type or "").strip(),
                "file_extension": str(file_extension or "").strip().lstrip("."),
            },
        )

    def upload_media(
        self,
        *,
        file_path: str | Path,
        parent_type: str,
        parent_node: str = "",
        file_name: str | None = None,
        extra: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Feishu media file not found: {path}")
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ValueError(f"Feishu media file is empty: {path}")
        if file_size > self.MAX_UPLOAD_BYTES:
            raise ValueError(f"Feishu media file exceeds 20MB upload_all limit: {path}")

        normalized_parent_type = str(parent_type or "").strip()
        if not normalized_parent_type:
            raise ValueError("Feishu media parent_type is required.")
        normalized_parent_node = str(parent_node or "").strip()
        if normalized_parent_type != "ccm_import_open" and not normalized_parent_node:
            raise ValueError("Feishu media parent_node is required for this parent_type.")

        name = str(file_name or path.name).strip() or path.name
        data = {
            "file_name": name,
            "parent_type": normalized_parent_type,
            "parent_node": normalized_parent_node,
            "size": str(file_size),
        }
        if extra is not None:
            data["extra"] = json.dumps(extra, ensure_ascii=False) if isinstance(extra, dict) else str(extra)

        access_token = self.auth_service.get_tenant_access_token()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with path.open("rb") as file_obj:
            response = self.client.client.post(
                self.UPLOAD_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
                data=data,
                files={"file": (name, file_obj, content_type)},
                timeout=float(settings.feishu_artifact_upload_timeout_seconds or 30.0),
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {payload.get('msg', 'unknown error')}")
        return self._normalize_upload_result(payload, file_name=name, file_size=file_size, parent_type=normalized_parent_type)

    @staticmethod
    def _normalize_upload_result(
        payload: dict[str, Any],
        *,
        file_name: str,
        file_size: int,
        parent_type: str,
    ) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        file_token = str(
            data.get("file_token")
            or data.get("file_key")
            or data.get("media_token")
            or data.get("token")
            or ""
        ).strip()
        if not file_token:
            raise RuntimeError("Feishu media upload response did not include a file token.")
        return {
            "file_token": file_token,
            "token": file_token,
            "file_name": str(data.get("file_name") or file_name),
            "size": int(data.get("size") or file_size),
            "parent_type": parent_type,
            "raw": payload,
        }
