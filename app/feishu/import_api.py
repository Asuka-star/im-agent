from __future__ import annotations

import time
from typing import Any

from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuImportAPI:
    """Creates and polls Feishu cloud document import tasks."""

    CREATE_IMPORT_TASK_PATH = "/open-apis/drive/v1/import_tasks"
    GET_IMPORT_TASK_PATH = "/open-apis/drive/v1/import_tasks/{ticket}"

    READY_STATUSES = {"0", "success", "succeeded", "done", "finished", "ready"}
    FAILED_STATUSES = {"2", "failed", "fail", "error", "cancelled", "canceled"}
    PENDING_STATUSES = {"1", "pending", "running", "processing", "queued", "created"}

    def __init__(
        self,
        *,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.auth_service = auth_service or FeishuAuthService()
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)

    def create_import_task(
        self,
        *,
        file_token: str,
        file_extension: str,
        import_type: str,
        file_name: str | None = None,
        folder_token: str | None = None,
    ) -> dict[str, Any]:
        normalized_file_token = str(file_token or "").strip()
        normalized_extension = str(file_extension or "").strip().lstrip(".")
        normalized_type = str(import_type or "").strip()
        if not normalized_file_token:
            raise ValueError("Feishu import file_token is required.")
        if not normalized_extension:
            raise ValueError("Feishu import file_extension is required.")
        if not normalized_type:
            raise ValueError("Feishu import type is required.")

        payload: dict[str, Any] = {
            "file_token": normalized_file_token,
            "file_extension": normalized_extension,
            "type": normalized_type,
        }
        normalized_file_name = str(file_name or "").strip()
        if normalized_file_name:
            payload["file_name"] = normalized_file_name
        mount_key = str(folder_token or settings.feishu_doc_folder_token or "").strip()
        if mount_key:
            payload["point"] = {"mount_type": 1, "mount_key": mount_key}

        response = self.client.post_json(
            self.CREATE_IMPORT_TASK_PATH,
            json=payload,
            headers=self._auth_headers(),
        )
        return self._normalize_import_result(response)

    def get_import_task(self, ticket: str) -> dict[str, Any]:
        normalized_ticket = str(ticket or "").strip()
        if not normalized_ticket:
            raise ValueError("Feishu import ticket is required.")
        response = self.client.get_json(
            self.GET_IMPORT_TASK_PATH.format(ticket=normalized_ticket),
            headers=self._auth_headers(),
        )
        return self._normalize_import_result(response, ticket=normalized_ticket)

    def wait_for_import(
        self,
        ticket: str,
        *,
        timeout_seconds: float | None = None,
        poll_seconds: float | None = None,
    ) -> dict[str, Any]:
        normalized_ticket = str(ticket or "").strip()
        if not normalized_ticket:
            raise ValueError("Feishu import ticket is required.")
        timeout = float(
            settings.feishu_artifact_slides_import_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        poll = max(
            0.0,
            float(
                settings.feishu_artifact_slides_import_poll_seconds
                if poll_seconds is None
                else poll_seconds
            ),
        )
        deadline = time.monotonic() + max(timeout, 0.0)
        last_result: dict[str, Any] | None = None
        while True:
            result = self.get_import_task(normalized_ticket)
            last_result = result
            status = str(result.get("status") or "").strip().lower()
            if status in {"ready", "failed"}:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Feishu import task timed out: ticket={normalized_ticket} status={status or 'unknown'}")
            if poll > 0:
                time.sleep(min(poll, max(deadline - time.monotonic(), 0.0)))
            else:
                time.sleep(0)

    def _auth_headers(self) -> dict[str, str]:
        token = self.auth_service.get_tenant_access_token()
        return {"Authorization": f"Bearer {token}"}

    @classmethod
    def _normalize_import_result(cls, payload: dict[str, Any], *, ticket: str | None = None) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        job = data.get("job") if isinstance(data.get("job"), dict) else {}
        sources = [data, result, task, job]

        resolved_ticket = str(
            cls._first_present(sources, "ticket", "job_id", "task_id", "import_task_id") or ticket or ""
        ).strip()
        raw_status = cls._first_present(sources, "job_status", "status", "state")
        normalized_status = cls._normalize_status(raw_status)
        token = str(
            cls._first_present(
                sources,
                "token",
                "file_token",
                "doc_token",
                "obj_token",
                "url_token",
                "document_id",
                "spreadsheet_token",
                "presentation_token",
            )
            or ""
        ).strip()
        url = str(
            cls._first_present(sources, "url", "document_url", "doc_url", "share_url", "obj_url")
            or ""
        ).strip()
        obj_type = str(cls._first_present(sources, "type", "obj_type") or "").strip()

        if not resolved_ticket:
            raise RuntimeError("Feishu import task response did not include a ticket.")
        return {
            "ticket": resolved_ticket,
            "status": normalized_status,
            "raw_status": raw_status,
            "token": token,
            "url": url,
            "type": obj_type,
            "raw": payload,
        }

    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        normalized = str(value).strip().lower() if value is not None else ""
        if normalized in cls.READY_STATUSES:
            return "ready"
        if normalized in cls.FAILED_STATUSES:
            return "failed"
        if normalized in cls.PENDING_STATUSES or not normalized:
            return "pending"
        return normalized

    @staticmethod
    def _first_present(sources: list[dict[str, Any]], *keys: str) -> Any:
        for source in sources:
            for key in keys:
                if key in source and source[key] is not None:
                    return source[key]
        return None
