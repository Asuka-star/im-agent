from __future__ import annotations

import logging
import re
import uuid
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

    def create_empty_document(self, title: str) -> dict[str, Any]:
        document_payload, folder_info = self._create_document_request(title)
        document_id, document_url = self._extract_document_info(document_payload)
        return {
            "document_id": document_id,
            "url": document_url,
            "title": title,
            "section_block_index": [],
            "folder_token": folder_info.get("token"),
            "folder_url": folder_info.get("url"),
            "folder_scope": folder_info.get("scope"),
            "folder_applied": bool(folder_info.get("applied")),
            "folder_note": folder_info.get("note"),
        }

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

    def append_blocks(
        self,
        document_id: str,
        children: list[dict[str, Any]],
        *,
        block_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        access_token = self.auth_service.get_tenant_access_token()
        target_block_id = block_id or document_id
        payload: dict[str, Any] = {"children": children}
        if index is not None:
            payload["index"] = max(int(index), 0)
        return self.client.post_json(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{target_block_id}/children",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"client_token": str(uuid.uuid4())},
            json=payload,
        )

    def list_child_blocks(self, document_id: str, block_id: str | None = None, *, page_size: int = 100) -> list[dict[str, Any]]:
        access_token = self.auth_service.get_tenant_access_token()
        target_block_id = block_id or document_id
        page_token = ""
        items: list[dict[str, Any]] = []
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            payload = self.client.get_json(
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{target_block_id}/children",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            page_items = data.get("items") if isinstance(data.get("items"), list) else []
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or data.get("next_page_token") or "").strip()
            if not page_token:
                break
        return items

    def delete_child_blocks(
        self,
        document_id: str,
        *,
        block_id: str | None = None,
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        access_token = self.auth_service.get_tenant_access_token()
        target_block_id = block_id or document_id
        return self.client.delete_json(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{target_block_id}/children/batch_delete",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"client_token": str(uuid.uuid4())},
            json={
                "start_index": max(int(start_index), 0),
                "end_index": max(int(end_index), 0),
            },
        )

    def update_text_block(self, document_id: str, block_id: str, content: str) -> dict[str, Any]:
        access_token = self.auth_service.get_tenant_access_token()
        return self.client.patch_json(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"client_token": str(uuid.uuid4())},
            json={
                "update_text_elements": {
                    "elements": self._text_data(content).get("elements", []),
                }
            },
        )

    def replace_image_block(self, document_id: str, block_id: str, token: str) -> dict[str, Any]:
        access_token = self.auth_service.get_tenant_access_token()
        return self.client.patch_json(
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"client_token": str(uuid.uuid4())},
            json={
                "replace_image": {
                    "token": token,
                }
            },
        )

    def create_document_from_sections(self, title: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
        created = self.create_empty_document(title)
        if not sections:
            return created
        appended = self.append_sections_to_document(str(created["document_id"]), title, sections)
        return {**created, **appended}

    def append_sections_to_document(self, document_id: str, title: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
        block_specs = self._build_block_specs(sections)
        blocks = [spec["block"] for spec in block_specs]
        appended_block_count = len(blocks)
        section_block_index: list[dict[str, Any]] = []
        if blocks:
            appended = self.append_blocks(document_id, blocks)
            created_blocks = self._extract_response_children(appended)
            appended_block_count += self._sync_created_child_blocks(
                document_id,
                block_specs,
                created_blocks,
            )
            section_block_index = self._section_block_index_from_created_blocks(sections, created_blocks)
        return {
            "document_id": document_id,
            "url": self._document_url(document_id),
            "title": title,
            "appended_block_count": appended_block_count,
            "section_block_index": section_block_index,
        }

    def replace_document_sections(
        self,
        document_id: str,
        title: str,
        sections: list[dict[str, Any]],
        *,
        target_headings: list[str] | None = None,
        delete_headings: list[str] | None = None,
        delete_ranges: list[dict[str, Any]] | None = None,
        append_headings: list[str] | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        root_children = self.list_child_blocks(document_id, document_id)
        ranges = self._section_ranges_from_blocks(root_children)
        range_by_heading = {item["heading"]: item for item in ranges}
        root_blocks_by_heading = {item["heading"]: root_children[int(item["start_index"])] for item in ranges if int(item["start_index"]) < len(root_children)}
        wanted = {heading for heading in target_headings if heading} if target_headings is not None else None
        delete_targets = {heading for heading in (delete_headings or []) if heading}
        rename_targets = {
            str(old_heading).strip(): str(new_heading).strip()
            for old_heading, new_heading in (rename_map or {}).items()
            if str(old_heading).strip() and str(new_heading).strip()
        }
        sections_to_write = [
            section
            for section in sections
            if isinstance(section, dict)
            and str(section.get("heading") or "").strip()
            and (wanted is None or str(section.get("heading") or "").strip() in wanted)
        ]

        replaced_count = 0
        inserted_count = 0
        replaced_headings: list[str] = []
        appended_headings: list[str] = []
        deleted_headings: list[str] = []
        renamed_headings: list[str] = []
        patched_headings: list[str] = []
        append_targets = {str(heading).strip() for heading in (append_headings or []) if str(heading).strip()}
        unmatched_update_headings: list[str] = []
        unmatched_delete_headings: list[str] = []
        unmatched_rename_headings: list[str] = []
        unmatched_delete_ranges: list[str] = []
        update_jobs: list[tuple[int, str, dict[str, Any]]] = []
        append_sections: list[dict] = []
        consumed_headings: set[str] = set()
        for section in sections_to_write:
            heading = str(section.get("heading") or "").strip()
            if heading in delete_targets:
                continue
            old_heading = next((source for source, target in rename_targets.items() if target == heading), None)
            current_range = range_by_heading.get(old_heading or heading)
            if current_range:
                update_jobs.append(
                    (
                        int(current_range["start_index"]),
                        "rename" if old_heading else "patch_body",
                        {
                            "range": current_range,
                            "section": section,
                            "old_heading": old_heading,
                            "new_heading": heading,
                        },
                    )
                )
                consumed_headings.add(heading)
                if old_heading:
                    consumed_headings.add(old_heading)
            elif old_heading:
                unmatched_rename_headings.append(old_heading)
            elif heading in append_targets or wanted is None:
                append_sections.append(section)
            else:
                unmatched_update_headings.append(heading)

        for heading in delete_targets:
            if heading in consumed_headings:
                continue
            current_range = range_by_heading.get(heading)
            if current_range:
                update_jobs.append(
                    (
                        int(current_range["start_index"]),
                        "delete",
                        {
                            "range": current_range,
                            "heading": heading,
                        },
                    )
                )
            else:
                unmatched_delete_headings.append(heading)

        for spec in delete_ranges or []:
            if not isinstance(spec, dict):
                continue
            label = self._semantic_delete_range_label(spec)
            semantic_range = self._resolve_semantic_delete_range(ranges, spec, total_blocks=len(root_children))
            if semantic_range is None:
                unmatched_delete_ranges.append(label)
                continue
            start_index, end_index, label = semantic_range
            if end_index <= start_index:
                unmatched_delete_ranges.append(label)
                continue
            update_jobs.append(
                (
                    start_index,
                    "delete_range",
                    {
                        "range": {"start_index": start_index, "end_index": end_index},
                        "heading": label,
                    },
                )
            )

        for _, job_type, payload in sorted(update_jobs, key=lambda item: item[0], reverse=True):
            current_range = payload["range"]
            start_index = int(current_range["start_index"])
            end_index = int(current_range["end_index"])
            if job_type in {"delete", "delete_range"}:
                if job_type == "delete":
                    start_index, end_index = self._expand_delete_range(
                        ranges,
                        heading=str(payload.get("heading") or ""),
                        start_index=start_index,
                        end_index=end_index,
                    )
                self.delete_child_blocks(
                    document_id,
                    block_id=document_id,
                    start_index=start_index,
                    end_index=end_index,
                )
                replaced_count += max(end_index - start_index, 0)
                deleted_headings.append(str(payload["heading"]))
                continue

            section = payload["section"]
            body_specs = self._section_body_block_specs(section)
            body_blocks = [spec["block"] for spec in body_specs]
            body_start_index = min(start_index + 1, end_index)
            if job_type == "rename":
                old_heading = str(payload.get("old_heading") or "").strip()
                new_heading = str(payload.get("new_heading") or "").strip()
                heading_block = root_blocks_by_heading.get(old_heading or new_heading)
                heading_block_id = str(heading_block.get("block_id") or "").strip() if isinstance(heading_block, dict) else ""
                if heading_block_id and new_heading:
                    self.update_text_block(document_id, heading_block_id, new_heading)
                    renamed_headings.append(f"{old_heading} -> {new_heading}")
            if end_index > body_start_index:
                self.delete_child_blocks(
                    document_id,
                    block_id=document_id,
                    start_index=body_start_index,
                    end_index=end_index,
                )
                replaced_count += max(end_index - body_start_index, 0)
            if body_blocks:
                inserted = self.append_blocks(document_id, body_blocks, block_id=document_id, index=body_start_index)
                created_blocks = self._extract_response_children(inserted)
                inserted_count += len(created_blocks) or len(body_blocks)
                inserted_count += self._sync_created_child_blocks(document_id, body_specs, created_blocks)
            patched_headings.append(str(section.get("heading") or "").strip())
            replaced_headings.append(str(section.get("heading") or "").strip())

        if append_sections:
            block_specs = self._build_block_specs(append_sections)
            blocks = [spec["block"] for spec in block_specs]
            if blocks:
                inserted = self.append_blocks(document_id, blocks, block_id=document_id)
                created_blocks = self._extract_response_children(inserted)
                inserted_count += len(created_blocks) or len(blocks)
                inserted_count += self._sync_created_child_blocks(document_id, block_specs, created_blocks)
                appended_headings = [str(section.get("heading") or "").strip() for section in append_sections]

        refreshed_children = self.list_child_blocks(document_id, document_id)
        return {
            "document_id": document_id,
            "url": self._document_url(document_id),
            "title": title,
            "replaced_block_count": replaced_count,
            "inserted_block_count": inserted_count,
            "replaced_headings": list(reversed(replaced_headings)),
            "appended_headings": appended_headings,
            "deleted_headings": list(reversed(deleted_headings)),
            "renamed_headings": list(reversed(renamed_headings)),
            "patched_headings": list(reversed(patched_headings)),
            "unmatched_update_headings": unmatched_update_headings,
            "unmatched_delete_headings": unmatched_delete_headings,
            "unmatched_rename_headings": unmatched_rename_headings,
            "unmatched_delete_ranges": unmatched_delete_ranges,
            "section_block_index": self._section_ranges_from_blocks(refreshed_children),
            "section_snapshot": self._section_snapshot_from_blocks(document_id, refreshed_children),
        }

    @staticmethod
    def _semantic_delete_range_label(spec: dict[str, Any]) -> str:
        label = str(spec.get("label") or "").strip()
        if label:
            return label
        anchor = str(spec.get("anchor") or spec.get("query") or "").strip()
        scope = str(spec.get("scope") or "").strip().lower()
        stop_at = str(spec.get("stop_at") or "").strip()
        if anchor and scope == "after":
            return f"{anchor} 之后"
        if anchor and scope == "before":
            return f"{anchor} 之前"
        if anchor and scope == "body":
            return f"{anchor} 正文"
        if anchor and scope == "group":
            return f"{anchor} 这一组"
        if anchor and scope == "between" and stop_at:
            return f"{anchor} 到 {stop_at} 之间"
        return anchor or "未命名范围"

    def _expand_delete_range(
        self,
        ranges: list[dict[str, Any]],
        *,
        heading: str,
        start_index: int,
        end_index: int,
    ) -> tuple[int, int]:
        """Expand semantic update-log sections to include their refresh subsections."""

        if not self._is_update_log_heading(heading):
            return start_index, end_index
        sorted_ranges = sorted(ranges, key=lambda item: int(item.get("start_index") or 0))
        seen_target = False
        expanded_end = end_index
        for item in sorted_ranges:
            current_start = int(item.get("start_index") or 0)
            current_end = int(item.get("end_index") or 0)
            current_heading = str(item.get("heading") or "").strip()
            if current_start < start_index:
                continue
            if current_start == start_index:
                seen_target = True
                expanded_end = max(expanded_end, current_end)
                continue
            if not seen_target:
                continue
            if self._is_update_log_heading(current_heading):
                break
            if not current_heading.lower().startswith("refresh:"):
                break
            expanded_end = max(expanded_end, current_end)
        return start_index, expanded_end

    def _resolve_semantic_delete_range(
        self,
        ranges: list[dict[str, Any]],
        spec: dict[str, Any],
        *,
        total_blocks: int,
    ) -> tuple[int, int, str] | None:
        scope = str(spec.get("scope") or "").strip().lower()
        anchor = str(spec.get("anchor") or spec.get("query") or "").strip()
        stop_at = str(spec.get("stop_at") or "").strip()
        if not scope or not anchor:
            return None
        range_by_heading = {str(item.get("heading") or "").strip(): item for item in ranges}
        anchor_range = range_by_heading.get(anchor)
        if not anchor_range:
            return None
        anchor_start = int(anchor_range.get("start_index") or 0)
        anchor_end = int(anchor_range.get("end_index") or anchor_start)
        include_anchor = bool(spec.get("include_anchor"))
        label = str(spec.get("label") or "").strip() or anchor

        if scope in {"section", "group"}:
            start_index, end_index = anchor_start, anchor_end
            if scope == "group":
                start_index, end_index = self._expand_delete_range(
                    ranges,
                    heading=anchor,
                    start_index=start_index,
                    end_index=end_index,
                )
            return start_index, end_index, label
        if scope == "body":
            return min(anchor_start + 1, anchor_end), anchor_end, label
        if scope == "after":
            return (anchor_start if include_anchor else anchor_end), total_blocks, label
        if scope == "before":
            return 0, (anchor_end if include_anchor else anchor_start), label
        if scope == "between":
            stop_range = range_by_heading.get(stop_at)
            if not stop_range:
                return None
            stop_start = int(stop_range.get("start_index") or 0)
            start_index = anchor_start if include_anchor else anchor_end
            return min(start_index, stop_start), max(start_index, stop_start), label
        return None

    @staticmethod
    def _is_update_log_heading(heading: str) -> bool:
        return bool(re.match(r"^Update\s*\([^)]+\)$", str(heading or "").strip(), re.IGNORECASE))

    def _section_body_block_specs(self, section: dict[str, Any]) -> list[dict[str, Any]]:
        paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
        specs: list[dict[str, Any]] = []
        for paragraph in paragraphs:
            specs.extend(self._paragraph_block_specs(paragraph))
        return specs

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
            url = self._document_url(str(document_id))
        return str(document_id), str(url)

    @staticmethod
    def _document_url(document_id: str) -> str:
        return f"https://feishu.cn/docx/{document_id}"

    def _build_blocks(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [spec["block"] for spec in self._build_block_specs(sections)]

    def _build_block_specs(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for section in sections:
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []

            if heading:
                specs.append(self._block_spec(self._heading_block(heading)))
            for paragraph in paragraphs:
                specs.extend(self._paragraph_block_specs(paragraph))
        return specs

    def _section_block_index_from_created_blocks(
        self,
        sections: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        index: list[dict[str, Any]] = []
        cursor = 0
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_blocks = self._build_blocks([section])
            count = len(section_blocks)
            if not count:
                continue
            block_slice = blocks[cursor : cursor + count]
            heading = str(section.get("heading") or "").strip()
            block_ids = [
                str(block.get("block_id") or "").strip()
                for block in block_slice
                if isinstance(block, dict) and str(block.get("block_id") or "").strip()
            ]
            index.append(
                {
                    "heading": heading,
                    "start_index": cursor,
                    "end_index": cursor + count,
                    "block_ids": block_ids,
                }
            )
            cursor += count
        return index

    def _section_ranges_from_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranges: list[dict[str, Any]] = []
        heading_positions: list[tuple[int, str]] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            block_type = int(block.get("block_type") or 0)
            if block_type not in range(3, 12):
                continue
            heading = self._block_text_content(block).strip()
            if heading:
                heading_positions.append((index, heading))

        for position, (start_index, heading) in enumerate(heading_positions):
            end_index = heading_positions[position + 1][0] if position + 1 < len(heading_positions) else len(blocks)
            block_slice = blocks[start_index:end_index]
            ranges.append(
                {
                    "heading": heading,
                    "start_index": start_index,
                    "end_index": end_index,
                    "block_ids": [
                        str(block.get("block_id") or "").strip()
                        for block in block_slice
                        if isinstance(block, dict) and str(block.get("block_id") or "").strip()
                    ],
                }
            )
        return ranges

    def _section_snapshot_from_blocks(self, document_id: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        ranges = self._section_ranges_from_blocks(blocks)
        for item in ranges:
            heading = str(item.get("heading") or "").strip()
            try:
                start_index = max(int(item.get("start_index") or 0), 0)
                end_index = max(int(item.get("end_index") or 0), 0)
            except (TypeError, ValueError):
                continue
            if not heading or end_index < start_index:
                continue
            paragraphs: list[Any] = []
            for block in blocks[start_index + 1 : end_index]:
                if not isinstance(block, dict):
                    continue
                paragraph = self._paragraph_snapshot_from_block(document_id, block)
                if paragraph is not None:
                    paragraphs.append(paragraph)
            snapshot.append({"heading": heading, "paragraphs": paragraphs})
        return snapshot

    def _paragraph_snapshot_from_block(self, document_id: str, block: dict[str, Any]) -> Any | None:
        block_type = int(block.get("block_type") or 0)
        if block_type == 22:
            return {"type": "divider"}
        if block_type == 27:
            image = block.get("image") if isinstance(block.get("image"), dict) else {}
            paragraph: dict[str, Any] = {"type": "image"}
            token = str(image.get("token") or "").strip()
            if token:
                paragraph["token"] = token
            width = self._safe_positive_int(image.get("width"))
            height = self._safe_positive_int(image.get("height"))
            if width is not None:
                paragraph["width"] = width
            if height is not None:
                paragraph["height"] = height
            return paragraph
        if block_type == 19:
            block_id = str(block.get("block_id") or "").strip()
            callout: dict[str, Any] = {"type": "callout"}
            text = self._child_block_text(document_id, block_id)
            if text:
                callout["text"] = text
            callout_data = block.get("callout") if isinstance(block.get("callout"), dict) else {}
            emoji_id = str(callout_data.get("emoji_id") or "").strip()
            if emoji_id:
                callout["emoji_id"] = emoji_id
            return callout
        if block_type == 31:
            rows = self._table_rows_from_block(document_id, block)
            if rows:
                return {"type": "table", "rows": rows}
            table = block.get("table") if isinstance(block.get("table"), dict) else {}
            props = table.get("property") if isinstance(table.get("property"), dict) else {}
            row_size = self._safe_positive_int(props.get("row_size")) or 1
            column_size = self._safe_positive_int(props.get("column_size")) or 1
            return {"type": "table", "rows": [[""] * column_size for _ in range(row_size)]}
        text = self._block_text_content(block).strip()
        return text or None

    def _child_block_text(self, document_id: str, block_id: str) -> str:
        if not block_id:
            return ""
        try:
            children = self.list_child_blocks(document_id, block_id)
        except Exception:  # noqa: BLE001
            return ""
        chunks: list[str] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            text = self._block_text_content(child).strip()
            if text:
                chunks.append(text)
        return "\n".join(chunks).strip()

    def _table_rows_from_block(self, document_id: str, block: dict[str, Any]) -> list[list[str]]:
        block_id = str(block.get("block_id") or "").strip()
        if not block_id:
            return []
        table = block.get("table") if isinstance(block.get("table"), dict) else {}
        props = table.get("property") if isinstance(table.get("property"), dict) else {}
        row_size = self._safe_positive_int(props.get("row_size")) or 0
        column_size = self._safe_positive_int(props.get("column_size")) or 0
        if row_size <= 0 or column_size <= 0:
            return []
        try:
            cells = self.list_child_blocks(document_id, block_id)
        except Exception:  # noqa: BLE001
            return []
        cell_texts: list[str] = []
        for cell in cells:
            if not isinstance(cell, dict) or int(cell.get("block_type") or 0) != 32:
                continue
            cell_id = str(cell.get("block_id") or "").strip()
            cell_texts.append(self._child_block_text(document_id, cell_id))
        rows: list[list[str]] = []
        cursor = 0
        for _ in range(row_size):
            row: list[str] = []
            for _ in range(column_size):
                row.append(cell_texts[cursor] if cursor < len(cell_texts) else "")
                cursor += 1
            rows.append(row)
        return rows

    def _extract_response_children(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        children = data.get("children") if isinstance(data.get("children"), list) else []
        return [item for item in children if isinstance(item, dict)]

    def _sync_created_child_blocks(
        self,
        document_id: str,
        block_specs: list[dict[str, Any]],
        created_blocks: list[dict[str, Any]],
    ) -> int:
        inserted_count = 0
        for spec, created in zip(block_specs, created_blocks):
            self._sync_created_block_post_create(document_id, spec, created)
            children = spec.get("children")
            if not isinstance(children, list) or not children:
                continue
            parent_block_id = str(created.get("block_id") or "").strip()
            if not parent_block_id:
                continue
            child_blocks = [
                child_spec["block"]
                for child_spec in children
                if isinstance(child_spec, dict) and isinstance(child_spec.get("block"), dict)
            ]
            if not child_blocks:
                continue
            appended = self.append_blocks(document_id, child_blocks, block_id=parent_block_id)
            created_children = self._extract_response_children(appended)
            inserted_count += len(created_children) or len(child_blocks)
            inserted_count += self._sync_created_child_blocks(document_id, children, created_children)
        return inserted_count

    def _sync_created_block_post_create(
        self,
        document_id: str,
        spec: dict[str, Any],
        created: dict[str, Any],
    ) -> None:
        post_create = spec.get("post_create") if isinstance(spec.get("post_create"), dict) else {}
        token = str(post_create.get("replace_image_token") or "").strip()
        if not token:
            return
        block_id = str(created.get("block_id") or "").strip()
        if not block_id:
            return
        try:
            self.replace_image_block(document_id, block_id, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to bind Feishu docx image token after block creation: document_id=%s block_id=%s error=%s",
                document_id,
                block_id,
                exc,
            )

    @staticmethod
    def _block_spec(
        block: dict[str, Any],
        *,
        children: list[dict[str, Any]] | None = None,
        post_create: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = {"block": block}
        if children:
            spec["children"] = children
        if post_create:
            spec["post_create"] = post_create
        return spec

    def _block_text_content(self, block: dict[str, Any]) -> str:
        for key in ("heading1", "heading2", "heading3", "heading4", "heading5", "heading6", "heading7", "heading8", "heading9", "text", "bullet", "ordered", "quote"):
            data = block.get(key)
            if isinstance(data, dict):
                return self._text_content(data)
        return ""

    def _text_content(self, data: dict[str, Any]) -> str:
        elements = data.get("elements") if isinstance(data.get("elements"), list) else []
        chunks: list[str] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            text_run = element.get("text_run") if isinstance(element.get("text_run"), dict) else {}
            content = str(text_run.get("content") or "")
            if content:
                chunks.append(content)
        return "".join(chunks)

    def _paragraph_blocks(self, paragraph: Any) -> list[dict[str, Any]]:
        return [spec["block"] for spec in self._paragraph_block_specs(paragraph)]

    def _paragraph_block_specs(self, paragraph: Any) -> list[dict[str, Any]]:
        if isinstance(paragraph, dict):
            blocks = self._structured_paragraph_block_specs(paragraph)
            if blocks:
                return blocks

        specs: list[dict[str, Any]] = []
        lines = [line.rstrip() for line in str(paragraph).splitlines() if line.strip()]
        line_index = 0
        while line_index < len(lines):
            content = lines[line_index].strip()
            unordered = re.match(r"^[-*]\s+(.+)$", content)
            if unordered:
                specs.append(self._block_spec(self._bullet_block(unordered.group(1).strip())))
                line_index += 1
                continue
            ordered = re.match(r"^\d+[\.)]\s+(.+)$", content)
            if ordered:
                specs.append(self._block_spec(self._ordered_block(ordered.group(1).strip())))
                line_index += 1
                continue
            quote = re.match(r"^>\s*(.+)$", content)
            if quote:
                specs.append(self._block_spec(self._quote_block(quote.group(1).strip())))
                line_index += 1
                continue
            if self._is_divider_line(content):
                specs.append(self._block_spec(self._divider_block()))
                line_index += 1
                continue
            image_match = re.match(r"^!\[(?P<alt>[^\]]*)\]\((?P<source>[^)]+)\)$", content)
            if image_match:
                specs.extend(
                    self._markdown_image_block_specs(
                        image_match.group("source").strip(),
                        alt_text=image_match.group("alt").strip(),
                    )
                )
                line_index += 1
                continue
            callout_match = re.match(r"^(?:\[!(?P<label>[A-Za-z]+)\]|(?P<prefix>NOTE|TIP|WARN|WARNING|INFO)):\s*(?P<body>.+)$", content, re.IGNORECASE)
            if callout_match:
                label = callout_match.group("label") or callout_match.group("prefix") or ""
                specs.append(
                    self._callout_block_spec(
                        callout_match.group("body").strip(),
                        kind=str(label).strip().lower(),
                    )
                )
                line_index += 1
                continue
            table_match = self._consume_markdown_table(lines, line_index)
            if table_match is not None:
                specs.append(self._table_block_spec(table_match["rows"]))
                line_index = int(table_match["next_index"])
                continue
            specs.append(self._block_spec(self._text_block(content)))
            line_index += 1
        return specs

    def _structured_paragraph_block_specs(self, paragraph: dict[str, Any]) -> list[dict[str, Any]]:
        paragraph_type = str(paragraph.get("type") or "").strip().lower()
        if paragraph_type == "divider":
            return [self._block_spec(self._divider_block())]
        if paragraph_type == "image":
            token = str(paragraph.get("token") or "").strip()
            if not token:
                return []
            bind_after_create = bool(paragraph.get("bind_after_create"))
            blocks = [
                self._block_spec(
                    self._image_block(
                        "" if bind_after_create else token,
                        width=self._safe_positive_int(paragraph.get("width")),
                        height=self._safe_positive_int(paragraph.get("height")),
                    ),
                    post_create={"replace_image_token": token} if bind_after_create else None,
                )
            ]
            caption = str(paragraph.get("caption") or paragraph.get("alt") or "").strip()
            if caption:
                blocks.append(self._block_spec(self._text_block(caption)))
            return blocks
        if paragraph_type == "table":
            rows = paragraph.get("rows")
            if isinstance(rows, list):
                normalized_rows = self._normalize_table_rows(rows)
                if normalized_rows:
                    return [self._table_block_spec(normalized_rows)]
        if paragraph_type in {"callout", "note"}:
            content = str(paragraph.get("text") or paragraph.get("content") or paragraph.get("caption") or "").strip()
            if not content:
                return []
            return [
                self._callout_block_spec(
                    content,
                    kind=str(paragraph.get("kind") or paragraph.get("variant") or paragraph_type).strip().lower(),
                    emoji_id=str(paragraph.get("emoji_id") or "").strip() or None,
                )
            ]
        return []

    @staticmethod
    def _is_divider_line(content: str) -> bool:
        return bool(re.match(r"^-{3,}$", content.strip()))

    def _consume_markdown_table(self, lines: list[str], start_index: int) -> dict[str, Any] | None:
        if start_index + 1 >= len(lines):
            return None
        header_line = lines[start_index].strip()
        separator_line = lines[start_index + 1].strip()
        if "|" not in header_line or "|" not in separator_line:
            return None
        if not re.match(r"^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", separator_line):
            return None

        table_lines = [header_line]
        next_index = start_index + 2
        while next_index < len(lines):
            candidate = lines[next_index].strip()
            if "|" not in candidate:
                break
            table_lines.append(candidate)
            next_index += 1

        rows = self._normalize_table_rows([self._split_markdown_table_row(line) for line in table_lines])
        if not rows:
            return None
        return {"rows": rows, "next_index": next_index}

    @staticmethod
    def _split_markdown_table_row(line: str) -> list[str]:
        text = line.strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        return [cell.strip() for cell in text.split("|")]

    def _normalize_table_rows(self, rows: list[Any]) -> list[list[str]]:
        normalized: list[list[str]] = []
        max_columns = 0
        for row in rows:
            if not isinstance(row, list):
                continue
            cells = [str(cell or "").strip() for cell in row]
            if not cells:
                continue
            normalized.append(cells)
            max_columns = max(max_columns, len(cells))
        if not normalized or max_columns <= 0:
            return []
        return [cells + [""] * (max_columns - len(cells)) for cells in normalized]

    def _markdown_image_block_specs(self, source: str, *, alt_text: str = "") -> list[dict[str, Any]]:
        token_match = re.match(r"^(?:token|image_token|img):(.+)$", source, re.IGNORECASE)
        if token_match:
            blocks = [self._block_spec(self._image_block(token_match.group(1).strip()))]
            if alt_text:
                blocks.append(self._block_spec(self._text_block(alt_text)))
            return blocks
        fallback = alt_text or source
        return [self._block_spec(self._text_block(f"[Image] {fallback}: {source}"))]

    @staticmethod
    def _heading_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 3,
            "heading1": FeishuDocAPI._text_data(content),
        }

    @staticmethod
    def _text_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 2,
            "text": FeishuDocAPI._text_data(content),
        }

    @staticmethod
    def _bullet_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 12,
            "bullet": FeishuDocAPI._text_data(content),
        }

    @staticmethod
    def _ordered_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 13,
            "ordered": FeishuDocAPI._text_data(content),
        }

    @staticmethod
    def _quote_block(content: str) -> dict[str, Any]:
        return {
            "block_type": 15,
            "quote": FeishuDocAPI._text_data(content),
        }

    @staticmethod
    def _divider_block() -> dict[str, Any]:
        return {
            "block_type": 22,
            "divider": {},
        }

    @staticmethod
    def _image_block(token: str, *, width: int | None = None, height: int | None = None) -> dict[str, Any]:
        image: dict[str, Any] = {}
        if token:
            image["token"] = token
        if width is not None:
            image["width"] = width
        if height is not None:
            image["height"] = height
        return {
            "block_type": 27,
            "image": image,
        }

    @staticmethod
    def _table_block(rows: list[list[str]]) -> dict[str, Any]:
        column_size = max((len(row) for row in rows), default=0)
        row_size = len(rows)
        return {
            "block_type": 31,
            "table": {
                "property": {
                    "row_size": row_size,
                    "column_size": column_size,
                }
            },
        }

    def _callout_block_spec(self, content: str, *, kind: str = "", emoji_id: str | None = None) -> dict[str, Any]:
        children = [self._block_spec(self._text_block(content))]
        return self._block_spec(
            self._callout_block(kind=kind, emoji_id=emoji_id),
            children=children,
        )

    @staticmethod
    def _callout_block(*, kind: str = "", emoji_id: str | None = None) -> dict[str, Any]:
        color_map = {
            "note": {"background_color": 2, "border_color": 2, "text_color": 2},
            "info": {"background_color": 2, "border_color": 2, "text_color": 2},
            "tip": {"background_color": 5, "border_color": 5, "text_color": 5},
            "warn": {"background_color": 14, "border_color": 14, "text_color": 14},
            "warning": {"background_color": 14, "border_color": 14, "text_color": 14},
        }
        callout = dict(color_map.get(kind.lower(), color_map["note"]))
        if emoji_id:
            callout["emoji_id"] = emoji_id
        return {
            "block_type": 19,
            "callout": callout,
        }

    def _table_block_spec(self, rows: list[list[str]]) -> dict[str, Any]:
        return self._block_spec(
            self._table_block(rows),
            children=[self._table_cell_block_spec(cell) for row in rows for cell in row],
        )

    def _table_cell_block_spec(self, content: str) -> dict[str, Any]:
        text = str(content or "")
        children: list[dict[str, Any]] = []
        if text:
            children.append(self._block_spec(self._text_block(text)))
        return self._block_spec(
            {
                "block_type": 32,
                "table_cell": {},
            },
            children=children,
        )

    @staticmethod
    def _safe_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _text_data(content: str) -> dict[str, Any]:
        return {
            "elements": [
                {
                    "text_run": {
                        "content": content,
                    }
                }
            ]
        }
