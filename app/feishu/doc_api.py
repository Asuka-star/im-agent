from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient
from app.services.app_state import AppStateService


logger = logging.getLogger(__name__)


class FeishuDocAPI:
    """Wraps Feishu Docx creation and basic content sync."""

    MANAGED_FOLDER_TOKEN_KEY = "feishu_doc_managed_folder_token"
    MANAGED_FOLDER_URL_KEY = "feishu_doc_managed_folder_url"

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
        state_service: AppStateService | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)
        self.state_service = state_service or AppStateService()

    def is_configured(self) -> bool:
        return bool(settings.feishu_doc_enabled)

    def create_document(self, title: str) -> dict[str, Any]:
        if not self.is_configured():
            raise RuntimeError("Feishu doc sync is not configured.")

        payload, _ = self._create_document_request(title)
        return payload

    def _create_document_request(self, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
        explicit_folder_token = (settings.feishu_doc_folder_token or "").strip()
        access_token = self.auth_service.get_tenant_access_token()
        payload: dict[str, Any] = {"title": title}
        folder_token = self._resolve_target_folder_token(access_token)
        if folder_token:
            payload["folder_token"] = folder_token

        folder_info = self._build_folder_result(folder_token, scope="explicit" if explicit_folder_token else "managed")

        try:
            response = self.client.post_json(
                "/open-apis/docx/v1/documents",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
            return response, folder_info
        except httpx.HTTPStatusError as exc:
            if explicit_folder_token and exc.response.status_code == 403:
                logger.warning(
                    "Feishu doc creation in folder failed with 403, retrying without folder_token: folder_token=%s",
                    explicit_folder_token,
                )
                response = self.client.post_json(
                    "/open-apis/docx/v1/documents",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"title": title},
                )
                return response, self._build_folder_result(
                    None,
                    scope="none",
                    note="指定目录不可写，本次已改为在默认位置创建文档。",
                )
            if not explicit_folder_token and folder_token and exc.response.status_code in {403, 404}:
                logger.warning(
                    "Managed Feishu doc folder became unavailable, recreating folder and retrying: folder_token=%s status=%s",
                    folder_token,
                    exc.response.status_code,
                )
                refreshed_folder_token = self._ensure_managed_folder(access_token, force_refresh=True)
                retry_payload: dict[str, Any] = {"title": title}
                if refreshed_folder_token:
                    retry_payload["folder_token"] = refreshed_folder_token
                response = self.client.post_json(
                    "/open-apis/docx/v1/documents",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=retry_payload,
                )
                return response, self._build_folder_result(refreshed_folder_token, scope="managed")
            raise

    def create_folder(self, name: str, *, parent_folder_token: str = "", access_token: str | None = None) -> dict[str, Any]:
        token = access_token or self.auth_service.get_tenant_access_token()
        return self.client.post_json(
            "/open-apis/drive/v1/files/create_folder",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": name,
                "folder_token": parent_folder_token,
            },
        )

    def append_blocks(self, document_id: str, children: list[dict[str, Any]], *, block_id: str | None = None) -> dict[str, Any]:
        access_token = self.auth_service.get_tenant_access_token()
        target_block_id = block_id or document_id
        return self.client.post_json(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{target_block_id}/children",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"children": children},
        )

    def create_document_from_sections(self, title: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
        document_payload, folder_info = self._create_document_request(title)
        document_id, document_url = self._extract_document_info(document_payload)
        blocks = self._build_blocks(sections)
        if blocks:
            self.append_blocks(document_id, blocks)
        return {
            "document_id": document_id,
            "url": document_url,
            "title": title,
            "folder_token": folder_info.get("token"),
            "folder_url": folder_info.get("url"),
            "folder_scope": folder_info.get("scope"),
            "folder_applied": bool(folder_info.get("applied")),
            "folder_note": folder_info.get("note"),
        }

    def _resolve_target_folder_token(self, access_token: str) -> str:
        explicit = (settings.feishu_doc_folder_token or "").strip()
        if explicit:
            return explicit
        return self._ensure_managed_folder(access_token)

    def _ensure_managed_folder(self, access_token: str, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self.state_service.get_value(self.MANAGED_FOLDER_TOKEN_KEY)
            if cached:
                return cached

        folder_name = (settings.feishu_doc_auto_folder_name or "").strip() or "AI协作产出"
        logger.info("Creating managed Feishu doc folder: name=%s", folder_name)
        created = self.create_folder(folder_name, parent_folder_token="", access_token=access_token)
        folder_data = created.get("data", {}) if isinstance(created, dict) else {}
        folder_token = str(folder_data.get("token") or "").strip()
        folder_url = str(folder_data.get("url") or "").strip()
        if not folder_token:
            raise RuntimeError("Feishu drive create_folder did not return a folder token.")
        self.state_service.set_value(self.MANAGED_FOLDER_TOKEN_KEY, folder_token)
        if folder_url:
            self.state_service.set_value(self.MANAGED_FOLDER_URL_KEY, folder_url)
        logger.info("Managed Feishu doc folder is ready: token=%s", folder_token)
        return folder_token

    def get_target_folder_info(self) -> dict[str, str | None]:
        explicit_token = (settings.feishu_doc_folder_token or "").strip()
        if explicit_token:
            return {
                "token": explicit_token,
                "url": f"https://feishu.cn/drive/folder/{explicit_token}",
            }

        managed_token = self.state_service.get_value(self.MANAGED_FOLDER_TOKEN_KEY)
        managed_url = self.state_service.get_value(self.MANAGED_FOLDER_URL_KEY)
        if managed_token:
            return {
                "token": managed_token,
                "url": managed_url or f"https://feishu.cn/drive/folder/{managed_token}",
            }
        return {"token": None, "url": None}

    def _build_folder_result(
        self,
        folder_token: str | None,
        *,
        scope: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        normalized_token = (folder_token or "").strip() or None
        if scope == "none" or not normalized_token:
            return {
                "token": None,
                "url": None,
                "scope": "none",
                "applied": False,
                "note": note,
            }

        if scope == "explicit":
            return {
                "token": normalized_token,
                "url": f"https://feishu.cn/drive/folder/{normalized_token}",
                "scope": "explicit",
                "applied": True,
                "note": note,
            }

        managed_url = self.state_service.get_value(self.MANAGED_FOLDER_URL_KEY)
        return {
            "token": normalized_token,
            "url": None,
            "scope": "managed",
            "applied": True,
            "note": note or "文档已归档到应用托管目录，当前未自动共享目录访问权限。",
            "managed_url": managed_url or f"https://feishu.cn/drive/folder/{normalized_token}",
        }

    def _extract_document_info(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        document = data.get("document", {}) if isinstance(data.get("document"), dict) else {}
        document_id = (
            document.get("document_id")
            or data.get("document_id")
            or data.get("docx_token")
            or document.get("docx_token")
        )
        if not document_id:
            raise RuntimeError("Feishu doc API did not return a document id.")

        url = document.get("url") or data.get("url")
        if not url:
            url = f"https://feishu.cn/docx/{document_id}"
        return str(document_id), str(url)

    def _build_blocks(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for section in sections:
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []

            if heading:
                blocks.append(self._heading_block(heading))
            for paragraph in paragraphs:
                content = str(paragraph).strip()
                if not content:
                    continue
                blocks.append(self._text_block(content))
        return blocks

    @staticmethod
    def _heading_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 3,
            "heading1": {
                "elements": [
                    {
                        "text_run": {
                            "content": content,
                        }
                    }
                ]
            },
        }

    @staticmethod
    def _text_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 2,
            "text": {
                "elements": [
                    {
                        "text_run": {
                            "content": content,
                        }
                    }
                ]
            },
        }
