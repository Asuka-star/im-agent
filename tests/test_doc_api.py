import unittest
from unittest.mock import patch

import httpx

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

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"path": path, "json": json, "headers": headers, "params": params})
        if path == "/open-apis/drive/v1/files/create_folder":
            return {"data": {"token": "managed-folder-token", "url": "https://feishu.cn/drive/folder/managed-folder-token"}}
        if path == "/open-apis/docx/v1/documents":
            return {"data": {"document": {"document_id": "doc-token", "url": "https://feishu.cn/docx/doc-token"}}}
        raise AssertionError(f"unexpected path: {path}")


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


if __name__ == "__main__":
    unittest.main()
