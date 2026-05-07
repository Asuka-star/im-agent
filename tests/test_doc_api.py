import unittest
from unittest.mock import MagicMock, patch

import httpx
import re

from app.feishu.doc_api import FeishuDocAPI


class DummyAuthService:
    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        return "token"


class DummyClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"path": path, "json": json, "headers": headers, "params": params})
        if len(self.calls) == 1:
            request = httpx.Request("POST", "https://open.feishu.cn/open-apis/docx/v1/documents")
            response = httpx.Response(status_code=403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)
        return {"data": {"document": {"document_id": "doc-token", "url": "https://feishu.cn/docx/doc-token"}}}


class ManagedFolderClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.child_counter = 0

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"path": path, "json": json, "headers": headers, "params": params})
        if path == "/open-apis/drive/v1/files/create_folder":
            return {"data": {"token": "managed-folder-token", "url": "https://feishu.cn/drive/folder/managed-folder-token"}}
        if path == "/open-apis/docx/v1/documents":
            return {"data": {"document": {"document_id": "doc-token", "url": "https://feishu.cn/docx/doc-token"}}}
        if re.match(r"^/open-apis/docx/v1/documents/[^/]+/blocks/[^/]+/children$", path):
            created = []
            for child in json.get("children", []):
                self.child_counter += 1
                clone = dict(child)
                clone["block_id"] = f"managed_{self.child_counter}"
                created.append(clone)
            return {"data": {"children": created}}
        raise AssertionError(f"unexpected path: {path}")

    def patch_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"path": path, "json": json, "headers": headers, "params": params, "method": "PATCH"})
        return {"data": {"block": {"block_id": path.split("/")[-1]}}}


class BlockOpsClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.child_counter = 0
        self.children = [
            _heading_block("h_summary", "讨论摘要"),
            _text_block("p_summary", "旧摘要"),
            _heading_block("h_risk", "风险与卡点"),
            _text_block("p_risk", "旧风险"),
            _heading_block("h_next", "下一步建议"),
            _text_block("p_next", "旧建议"),
        ]

    def get_json(self, path: str, *, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"method": "GET", "path": path, "headers": headers, "params": params})
        return {"data": {"has_more": False, "items": list(self.children)}}

    def delete_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"method": "DELETE", "path": path, "json": json, "headers": headers, "params": params})
        del self.children[json["start_index"] : json["end_index"]]
        return {"data": {"document_revision_id": 2}}

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"method": "POST", "path": path, "json": json, "headers": headers, "params": params})
        created = []
        for index, child in enumerate(json.get("children", []), start=1):
            clone = dict(child)
            self.child_counter += 1
            clone["block_id"] = f"new_{self.child_counter}"
            created.append(clone)
        if path.endswith("/blocks/doc-token/children"):
            insert_index = json.get("index")
            if insert_index is None:
                self.children.extend(created)
            else:
                self.children[insert_index:insert_index] = created
        return {"data": {"children": created, "document_revision_id": 3}}

    def patch_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"method": "PATCH", "path": path, "json": json, "headers": headers, "params": params})
        block_id = path.split("/")[-1]
        updated_content = json["update_text_elements"]["elements"][0]["text_run"]["content"]
        for child in self.children:
            if child.get("block_id") == block_id:
                key = "heading1" if child.get("block_type") == 3 else "text"
                child[key] = {"elements": [{"text_run": {"content": updated_content}}]}
                break
        return {"data": {"block": {"block_id": block_id}}}


class AsciiBlockOpsClient(BlockOpsClient):
    def __init__(self) -> None:
        super().__init__()
        self.children = [
            _heading_block("h_summary_ascii", "Summary"),
            _text_block("p_summary_ascii", "Old summary"),
            _heading_block("h_risk_ascii", "Risks"),
            _text_block("p_risk_ascii", "Old risk"),
            _heading_block("h_next_ascii", "Next Steps"),
            _text_block("p_next_ascii", "Old next step"),
        ]


class UpdateLogBlockOpsClient(BlockOpsClient):
    def __init__(self) -> None:
        super().__init__()
        self.children = [
            _heading_block("h_intro", "Project Background"),
            _text_block("p_intro", "Keep intro"),
            _heading_block("h_update_1", "Update (2026-04-29 15:43)"),
            _text_block("p_trigger_1", "Trigger: old summary"),
            _heading_block("h_refresh_1", "refresh: Project Background"),
            _text_block("p_refresh_1", "Old generated background"),
            _heading_block("h_refresh_2", "refresh: Risks"),
            _text_block("p_refresh_2", "Old generated risk"),
            _heading_block("h_update_2", "Update (2026-04-29 16:09)"),
            _text_block("p_trigger_2", "Trigger: newer update"),
            _heading_block("h_current", "Current Tasks"),
            _text_block("p_current", "Keep current tasks"),
        ]


def _heading_block(block_id: str, content: str) -> dict:
    return {
        "block_id": block_id,
        "block_type": 3,
        "heading1": {"elements": [{"text_run": {"content": content}}]},
    }


def _text_block(block_id: str, content: str) -> dict:
    return {
        "block_id": block_id,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": content}}]},
    }


class DummyStateService:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class DocApiTests(unittest.TestCase):
    def test_create_document_retries_without_folder_when_folder_forbidden(self) -> None:
        client = DummyClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        with patch("app.feishu.doc_api.settings.feishu_doc_enabled", True), patch(
            "app.feishu.doc_api.settings.feishu_doc_folder_token",
            "folder-token",
        ):
            payload = api.create_document("测试文档")

        self.assertEqual(payload["data"]["document"]["document_id"], "doc-token")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["json"]["folder_token"], "folder-token")
        self.assertEqual(client.calls[1]["json"], {"title": "测试文档"})

    def test_create_document_auto_creates_managed_folder_and_persists_token(self) -> None:
        client = ManagedFolderClient()
        state = DummyStateService()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=state)

        with patch("app.feishu.doc_api.settings.feishu_doc_enabled", True), patch(
            "app.feishu.doc_api.settings.feishu_doc_folder_token",
            "",
        ), patch("app.feishu.doc_api.settings.feishu_doc_auto_folder_name", "AI协作产出"):
            payload = api.create_document("测试文档")

        self.assertEqual(payload["data"]["document"]["document_id"], "doc-token")
        self.assertEqual(state.values[api.MANAGED_FOLDER_TOKEN_KEY], "managed-folder-token")
        self.assertEqual(state.values[api.MANAGED_FOLDER_URL_KEY], "https://feishu.cn/drive/folder/managed-folder-token")
        self.assertEqual(client.calls[0]["path"], "/open-apis/drive/v1/files/create_folder")
        self.assertEqual(client.calls[0]["json"], {"name": "AI协作产出", "folder_token": ""})
        self.assertEqual(client.calls[1]["path"], "/open-apis/docx/v1/documents")
        self.assertEqual(client.calls[1]["json"]["folder_token"], "managed-folder-token")

    def test_create_document_reuses_cached_managed_folder_token(self) -> None:
        client = ManagedFolderClient()
        state = DummyStateService()
        state.set_value(FeishuDocAPI.MANAGED_FOLDER_TOKEN_KEY, "cached-folder-token")
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=state)

        with patch("app.feishu.doc_api.settings.feishu_doc_enabled", True), patch(
            "app.feishu.doc_api.settings.feishu_doc_folder_token",
            "",
        ):
            payload = api.create_document("测试文档")

        self.assertEqual(payload["data"]["document"]["document_id"], "doc-token")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["path"], "/open-apis/docx/v1/documents")
        self.assertEqual(client.calls[0]["json"]["folder_token"], "cached-folder-token")

    def test_get_target_folder_info_builds_folder_url(self) -> None:
        state = DummyStateService()
        state.set_value(FeishuDocAPI.MANAGED_FOLDER_TOKEN_KEY, "cached-folder-token")
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=state)

        info = api.get_target_folder_info()

        self.assertEqual(info["token"], "cached-folder-token")
        self.assertEqual(info["url"], "https://feishu.cn/drive/folder/cached-folder-token")

    def test_create_document_from_sections_reports_fallback_when_explicit_folder_forbidden(self) -> None:
        client = DummyClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        with patch("app.feishu.doc_api.settings.feishu_doc_enabled", True), patch(
            "app.feishu.doc_api.settings.feishu_doc_folder_token",
            "folder-token",
        ):
            payload = api.create_document_from_sections("测试文档", [])

        self.assertEqual(payload["document_id"], "doc-token")
        self.assertFalse(payload["folder_applied"])
        self.assertEqual(payload["folder_scope"], "none")
        self.assertIsNone(payload["folder_url"])
        self.assertIn("默认位置", payload["folder_note"])

    def test_create_document_from_sections_uses_managed_folder_note_without_link(self) -> None:
        client = ManagedFolderClient()
        state = DummyStateService()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=state)

        with patch("app.feishu.doc_api.settings.feishu_doc_enabled", True), patch(
            "app.feishu.doc_api.settings.feishu_doc_folder_token",
            "",
        ), patch("app.feishu.doc_api.settings.feishu_doc_auto_folder_name", "AI协作产出"):
            payload = api.create_document_from_sections("测试文档", [])

        self.assertEqual(payload["document_id"], "doc-token")
        self.assertTrue(payload["folder_applied"])
        self.assertEqual(payload["folder_scope"], "managed")
        self.assertIsNone(payload["folder_url"])
        self.assertIn("未自动共享", payload["folder_note"])


    def test_append_sections_to_document_appends_children(self) -> None:
        client = ManagedFolderClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.append_sections_to_document(
            "doc-token",
            "测试文档",
            [{"heading": "补充", "paragraphs": ["第一条", "第二条"]}],
        )

        self.assertEqual(payload["document_id"], "doc-token")
        self.assertEqual(payload["url"], "https://feishu.cn/docx/doc-token")
        self.assertEqual(payload["appended_block_count"], 3)
        self.assertEqual(client.calls[0]["path"], "/open-apis/docx/v1/documents/doc-token/blocks/doc-token/children")

    def test_append_sections_to_document_populates_table_cells(self) -> None:
        client = ManagedFolderClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.append_sections_to_document(
            "doc-token",
            "测试文档",
            [{"heading": "状态", "paragraphs": [{"type": "table", "rows": [["模块", "状态"], ["DocTool", "Ready"]]}]}],
        )

        self.assertEqual(payload["appended_block_count"], 10)
        child_paths = [call["path"] for call in client.calls]
        self.assertIn("/open-apis/docx/v1/documents/doc-token/blocks/managed_2/children", child_paths)
        self.assertIn("/open-apis/docx/v1/documents/doc-token/blocks/managed_3/children", child_paths)
        cell_text_call = next(
            call for call in client.calls if call["path"] == "/open-apis/docx/v1/documents/doc-token/blocks/managed_3/children"
        )
        self.assertEqual(
            cell_text_call["json"]["children"][0]["text"]["elements"][0]["text_run"]["content"],
            "模块",
        )

    def test_replace_document_sections_deletes_and_recreates_matching_section(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [{"heading": "风险与卡点", "paragraphs": ["新风险"]}],
            target_headings=["风险与卡点"],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        post_call = next(call for call in client.calls if call["method"] == "POST")
        self.assertEqual(delete_call["json"], {"start_index": 3, "end_index": 4})
        self.assertEqual(post_call["json"]["index"], 3)
        self.assertEqual(payload["replaced_block_count"], 1)
        self.assertEqual(payload["inserted_block_count"], 1)
        self.assertEqual(payload["replaced_headings"], ["风险与卡点"])
        self.assertEqual(payload["section_block_index"][1]["heading"], "风险与卡点")
        self.assertEqual(payload["section_block_index"][1]["block_ids"], ["h_risk", "new_1"])

    def test_replace_document_sections_can_delete_section(self) -> None:
        client = AsciiBlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "Test Document",
            [],
            delete_headings=["Next Steps"],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 4, "end_index": 6})
        self.assertEqual(payload["deleted_headings"], ["Next Steps"])

    def test_replace_document_sections_deletes_whole_update_log_group(self) -> None:
        client = UpdateLogBlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "Test Document",
            [],
            delete_headings=["Update (2026-04-29 15:43)"],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 2, "end_index": 8})
        remaining_headings = [
            api._block_text_content(block)
            for block in client.children
            if int(block.get("block_type") or 0) in range(3, 12)
        ]
        self.assertEqual(remaining_headings, ["Project Background", "Update (2026-04-29 16:09)", "Current Tasks"])
        self.assertEqual(payload["deleted_headings"], ["Update (2026-04-29 15:43)"])

    def test_replace_document_sections_deletes_after_anchor_range(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [],
            delete_ranges=[
                {
                    "scope": "after",
                    "anchor": "风险与卡点",
                    "include_anchor": False,
                    "label": "风险与卡点 之后",
                }
            ],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 4, "end_index": 6})
        remaining_headings = [
            api._block_text_content(block)
            for block in client.children
            if int(block.get("block_type") or 0) in range(3, 12)
        ]
        self.assertEqual(remaining_headings, ["讨论摘要", "风险与卡点"])
        self.assertEqual(payload["deleted_headings"], ["风险与卡点 之后"])
        self.assertEqual(
            payload["section_snapshot"],
            [
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "风险与卡点", "paragraphs": ["旧风险"]},
            ],
        )

    def test_replace_document_sections_reports_unmatched_delete_range(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [],
            delete_ranges=[
                {
                    "scope": "after",
                    "anchor": "后续计划",
                    "include_anchor": False,
                    "label": "后续计划 之后",
                }
            ],
        )

        self.assertFalse(any(call.get("method") == "DELETE" for call in client.calls))
        remaining_headings = [
            api._block_text_content(block)
            for block in client.children
            if int(block.get("block_type") or 0) in range(3, 12)
        ]
        self.assertEqual(remaining_headings, ["讨论摘要", "风险与卡点", "下一步建议"])
        self.assertEqual(payload["deleted_headings"], [])
        self.assertEqual(payload["unmatched_delete_ranges"], ["后续计划 之后"])

    def test_replace_document_sections_reports_unmatched_update_target(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [{"heading": "后续计划", "paragraphs": ["新计划"]}],
            target_headings=["后续计划"],
        )

        self.assertFalse(any(call.get("method") == "POST" for call in client.calls))
        self.assertEqual(payload["appended_headings"], [])
        self.assertEqual(payload["unmatched_update_headings"], ["后续计划"])

    def test_replace_document_sections_allows_explicit_append_target(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [{"heading": "验收标准", "paragraphs": ["新增验收"]}],
            target_headings=["验收标准"],
            append_headings=["验收标准"],
        )

        post_call = next(call for call in client.calls if call.get("method") == "POST")
        self.assertEqual(len(post_call["json"]["children"]), 2)
        self.assertEqual(payload["appended_headings"], ["验收标准"])
        self.assertEqual(payload["unmatched_update_headings"], [])

    def test_replace_document_sections_reports_unmatched_rename_source(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [{"heading": "关键风险", "paragraphs": ["新风险"]}],
            target_headings=["关键风险"],
            rename_map={"不存在的风险": "关键风险"},
        )

        self.assertFalse(any(call.get("method") == "PATCH" for call in client.calls))
        self.assertFalse(any(call.get("method") == "POST" for call in client.calls))
        self.assertEqual(payload["renamed_headings"], [])
        self.assertEqual(payload["unmatched_rename_headings"], ["不存在的风险"])

    def test_replace_document_sections_clears_anchor_body(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [],
            delete_ranges=[
                {
                    "scope": "body",
                    "anchor": "风险与卡点",
                    "include_anchor": False,
                    "label": "风险与卡点 正文",
                }
            ],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 3, "end_index": 4})
        remaining_headings = [
            api._block_text_content(block)
            for block in client.children
            if int(block.get("block_type") or 0) in range(3, 12)
        ]
        self.assertEqual(remaining_headings, ["讨论摘要", "风险与卡点", "下一步建议"])
        self.assertEqual(payload["deleted_headings"], ["风险与卡点 正文"])

    def test_replace_document_sections_deletes_between_anchors(self) -> None:
        client = BlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "测试文档",
            [],
            delete_ranges=[
                {
                    "scope": "between",
                    "anchor": "讨论摘要",
                    "stop_at": "下一步建议",
                    "include_anchor": False,
                    "label": "讨论摘要 到 下一步建议 之间",
                }
            ],
        )

        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 2, "end_index": 4})
        remaining_headings = [
            api._block_text_content(block)
            for block in client.children
            if int(block.get("block_type") or 0) in range(3, 12)
        ]
        self.assertEqual(remaining_headings, ["讨论摘要", "下一步建议"])
        self.assertEqual(payload["deleted_headings"], ["讨论摘要 到 下一步建议 之间"])

    def test_replace_document_sections_can_rename_heading_and_patch_body(self) -> None:
        client = AsciiBlockOpsClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.replace_document_sections(
            "doc-token",
            "Test Document",
            [{"heading": "Key Risks", "paragraphs": ["New risk"]}],
            target_headings=["Key Risks"],
            rename_map={"Risks": "Key Risks"},
        )

        patch_call = next(call for call in client.calls if call["method"] == "PATCH")
        self.assertIn("/blocks/h_risk_ascii", patch_call["path"])
        self.assertEqual(
            patch_call["json"]["update_text_elements"]["elements"][0]["text_run"]["content"],
            "Key Risks",
        )
        delete_call = next(call for call in client.calls if call["method"] == "DELETE")
        self.assertEqual(delete_call["json"], {"start_index": 3, "end_index": 4})
        self.assertEqual(payload["renamed_headings"], ["Risks -> Key Risks"])
        self.assertEqual(payload["patched_headings"], ["Key Risks"])

    def test_build_blocks_converts_markdown_like_paragraphs_to_rich_blocks(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        blocks = api._build_blocks(
            [
                {
                    "heading": "补充",
                    "paragraphs": [
                        "- 无序事项",
                        "1. 有序事项",
                        "> 风险提示",
                        "普通段落",
                    ],
                }
            ]
        )

        self.assertEqual([block["block_type"] for block in blocks], [3, 12, 13, 15, 2])
        self.assertEqual(blocks[1]["bullet"]["elements"][0]["text_run"]["content"], "无序事项")
        self.assertEqual(blocks[2]["ordered"]["elements"][0]["text_run"]["content"], "有序事项")
        self.assertEqual(blocks[3]["quote"]["elements"][0]["text_run"]["content"], "风险提示")

    def test_build_blocks_splits_multiline_paragraphs(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        blocks = api._build_blocks([{"heading": "", "paragraphs": ["第一段\n- 第二段"]}])

        self.assertEqual([block["block_type"] for block in blocks], [2, 12])
        self.assertEqual(blocks[0]["text"]["elements"][0]["text_run"]["content"], "第一段")
        self.assertEqual(blocks[1]["bullet"]["elements"][0]["text_run"]["content"], "第二段")


    def test_build_blocks_supports_divider_image_and_markdown_table(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        blocks = api._build_blocks(
            [
                {
                    "heading": "Artifacts",
                    "paragraphs": [
                        "---",
                        "![架构图](token:img_token_123)",
                        "| 模块 | 状态 |\n| --- | --- |\n| Planner | Done |\n| DocTool | Doing |",
                    ],
                }
            ]
        )

        self.assertEqual([block["block_type"] for block in blocks], [3, 22, 27, 2, 31])
        self.assertEqual(blocks[2]["image"]["token"], "img_token_123")
        self.assertEqual(blocks[3]["text"]["elements"][0]["text_run"]["content"], "架构图")
        self.assertEqual(blocks[4]["table"]["property"]["row_size"], 3)
        self.assertEqual(blocks[4]["table"]["property"]["column_size"], 2)

    def test_build_blocks_supports_structured_image_and_divider_paragraphs(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        blocks = api._build_blocks(
            [
                {
                    "heading": "",
                    "paragraphs": [
                        {"type": "divider"},
                        {"type": "image", "token": "img_token_456", "width": 640, "height": 320, "caption": "流程图"},
                        {"type": "table", "rows": [["Name", "Status"], ["DocTool", "Ready"]]},
                    ],
                }
            ]
        )

        self.assertEqual([block["block_type"] for block in blocks], [22, 27, 2, 31])
        self.assertEqual(blocks[1]["image"]["width"], 640)
        self.assertEqual(blocks[1]["image"]["height"], 320)
        self.assertEqual(blocks[2]["text"]["elements"][0]["text_run"]["content"], "流程图")
        self.assertEqual(blocks[3]["table"]["property"]["row_size"], 2)
        self.assertEqual(blocks[3]["table"]["property"]["column_size"], 2)

    def test_append_sections_binds_image_token_after_block_creation(self) -> None:
        client = ManagedFolderClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.append_sections_to_document(
            "doc-token",
            "测试文档",
            [
                {
                    "heading": "Canvas",
                    "paragraphs": [
                        {
                            "type": "image",
                            "token": "img_token_uploaded",
                            "bind_after_create": True,
                            "caption": "流程图",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(payload["appended_block_count"], 3)
        post_call = next(
            call
            for call in client.calls
            if call["path"] == "/open-apis/docx/v1/documents/doc-token/blocks/doc-token/children"
        )
        image_child = post_call["json"]["children"][1]
        self.assertEqual(image_child["block_type"], 27)
        self.assertEqual(image_child["image"], {})
        patch_call = next(call for call in client.calls if call.get("method") == "PATCH")
        self.assertIn("/blocks/managed_2", patch_call["path"])
        self.assertEqual(patch_call["json"], {"replace_image": {"token": "img_token_uploaded"}})

    def test_build_blocks_supports_markdown_and_structured_callout(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        blocks = api._build_blocks(
            [
                {
                    "heading": "",
                    "paragraphs": [
                        "[!NOTE]: Re-run the full regression before release.",
                        {"type": "callout", "text": "Watch the risk section during rollout.", "kind": "warning", "emoji_id": "warning"},
                    ],
                }
            ]
        )

        self.assertEqual([block["block_type"] for block in blocks], [19, 19])
        specs = api._build_block_specs(
            [
                {
                    "heading": "",
                    "paragraphs": [
                        "[!NOTE]: Re-run the full regression before release.",
                        {"type": "callout", "text": "Watch the risk section during rollout.", "kind": "warning", "emoji_id": "warning"},
                    ],
                }
            ]
        )
        self.assertEqual(
            specs[0]["children"][0]["block"]["text"]["elements"][0]["text_run"]["content"],
            "Re-run the full regression before release.",
        )
        self.assertEqual(specs[1]["block"]["callout"]["emoji_id"], "warning")

    def test_build_block_specs_attach_table_cell_children(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())

        specs = api._build_block_specs(
            [{"heading": "状态", "paragraphs": [{"type": "table", "rows": [["模块", "状态"], ["DocTool", "Ready"]]}]}]
        )

        self.assertEqual(len(specs), 2)
        table_spec = specs[1]
        self.assertEqual(table_spec["block"]["block_type"], 31)
        self.assertEqual(len(table_spec["children"]), 4)
        first_cell = table_spec["children"][0]
        self.assertEqual(first_cell["block"]["block_type"], 32)
        self.assertEqual(first_cell["children"][0]["block"]["text"]["elements"][0]["text_run"]["content"], "模块")

    def test_append_sections_to_document_populates_callout_children(self) -> None:
        client = ManagedFolderClient()
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=client, state_service=DummyStateService())

        payload = api.append_sections_to_document(
            "doc-token",
            "Test Document",
            [{"heading": "Notes", "paragraphs": [{"type": "callout", "text": "Double check the rollout window.", "kind": "note"}]}],
        )

        self.assertEqual(payload["appended_block_count"], 3)
        child_paths = [call["path"] for call in client.calls]
        self.assertIn("/open-apis/docx/v1/documents/doc-token/blocks/managed_2/children", child_paths)

    def test_section_snapshot_from_blocks_preserves_rich_block_structure(self) -> None:
        api = FeishuDocAPI(auth_service=DummyAuthService(), client=ManagedFolderClient(), state_service=DummyStateService())
        child_map = {
            "callout_1": [_text_block("callout_text", "Double check the rollout window.")],
            "table_1": [
                {"block_id": "cell_1", "block_type": 32, "table_cell": {}},
                {"block_id": "cell_2", "block_type": 32, "table_cell": {}},
                {"block_id": "cell_3", "block_type": 32, "table_cell": {}},
                {"block_id": "cell_4", "block_type": 32, "table_cell": {}},
            ],
            "cell_1": [_text_block("cell_1_text", "Module")],
            "cell_2": [_text_block("cell_2_text", "Status")],
            "cell_3": [_text_block("cell_3_text", "DocTool")],
            "cell_4": [_text_block("cell_4_text", "Ready")],
        }
        api.list_child_blocks = MagicMock(side_effect=lambda document_id, block_id=None, page_size=100: child_map.get(block_id or document_id, []))

        snapshot = api._section_snapshot_from_blocks(
            "doc-token",
            [
                _heading_block("heading_1", "Artifacts"),
                {"block_id": "divider_1", "block_type": 22, "divider": {}},
                {"block_id": "image_1", "block_type": 27, "image": {"token": "img_token_1", "width": 640, "height": 320}},
                {"block_id": "callout_1", "block_type": 19, "callout": {"emoji_id": "warning"}},
                {"block_id": "table_1", "block_type": 31, "table": {"property": {"row_size": 2, "column_size": 2}}},
            ],
        )

        self.assertEqual(snapshot[0]["heading"], "Artifacts")
        self.assertEqual(snapshot[0]["paragraphs"][0]["type"], "divider")
        self.assertEqual(snapshot[0]["paragraphs"][1]["type"], "image")
        self.assertEqual(snapshot[0]["paragraphs"][1]["token"], "img_token_1")
        self.assertEqual(snapshot[0]["paragraphs"][2]["type"], "callout")
        self.assertEqual(snapshot[0]["paragraphs"][2]["text"], "Double check the rollout window.")
        self.assertEqual(snapshot[0]["paragraphs"][3]["type"], "table")
        self.assertEqual(snapshot[0]["paragraphs"][3]["rows"], [["Module", "Status"], ["DocTool", "Ready"]])



if __name__ == "__main__":
    unittest.main()
