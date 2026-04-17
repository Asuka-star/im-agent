from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuDocAPI:
    """Wraps Feishu Docx creation and basic content sync."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def is_configured(self) -> bool:
        return bool(settings.feishu_doc_enabled)

    def create_document(self, title: str) -> dict[str, Any]:
        if not self.is_configured():
            raise RuntimeError("Feishu doc sync is not configured.")

        access_token = self.auth_service.get_tenant_access_token()
        payload: dict[str, Any] = {"title": title}
        if settings.feishu_doc_folder_token:
            payload["folder_token"] = settings.feishu_doc_folder_token

        return self.client.post_json(
            "/open-apis/docx/v1/documents",
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
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
        document_payload = self.create_document(title)
        document_id, document_url = self._extract_document_info(document_payload)
        blocks = self._build_blocks(sections)
        if blocks:
            self.append_blocks(document_id, blocks)
        return {
            "document_id": document_id,
            "url": document_url,
            "title": title,
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
