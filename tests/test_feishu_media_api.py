import tempfile
import unittest
from pathlib import Path

from app.feishu.media_api import FeishuMediaAPI


class DummyAuthService:
    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        return "tenant-token"


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class DummyHTTPClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, path: str, **kwargs):
        file_tuple = kwargs["files"]["file"]
        self.calls.append(
            {
                "path": path,
                "headers": kwargs.get("headers"),
                "data": kwargs.get("data"),
                "file_name": file_tuple[0],
                "content_type": file_tuple[2],
                "file_bytes": file_tuple[1].read(),
                "timeout": kwargs.get("timeout"),
            }
        )
        return DummyResponse({"code": 0, "data": {"file_token": "img_token_1", "file_name": file_tuple[0]}})


class DummyClient:
    def __init__(self) -> None:
        self.client = DummyHTTPClient()


class FeishuMediaAPITests(unittest.TestCase):
    def test_upload_docx_image_posts_multipart_payload(self) -> None:
        client = DummyClient()
        api = FeishuMediaAPI(auth_service=DummyAuthService(), client=client)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "canvas.svg"
            path.write_text("<svg></svg>", encoding="utf-8")

            result = api.upload_docx_image(document_id="doc_1", file_path=path)

        self.assertEqual(result["file_token"], "img_token_1")
        call = client.client.calls[0]
        self.assertEqual(call["path"], "/open-apis/drive/v1/medias/upload_all")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tenant-token")
        self.assertEqual(call["data"]["parent_type"], "docx_image")
        self.assertEqual(call["data"]["parent_node"], "doc_1")
        self.assertEqual(call["data"]["file_name"], "canvas.svg")
        self.assertEqual(call["file_bytes"], b"<svg></svg>")

    def test_upload_docx_image_requires_existing_file(self) -> None:
        api = FeishuMediaAPI(auth_service=DummyAuthService(), client=DummyClient())

        with self.assertRaises(FileNotFoundError):
            api.upload_docx_image(document_id="doc_1", file_path="missing.svg")


if __name__ == "__main__":
    unittest.main()
