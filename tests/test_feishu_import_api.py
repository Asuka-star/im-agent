import unittest

from app.feishu.import_api import FeishuImportAPI


class DummyAuthService:
    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        return "tenant-token"


class DummyClient:
    def __init__(self) -> None:
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.get_payloads: list[dict] = []

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.post_calls.append({"path": path, "json": json, "headers": headers, "params": params})
        return {"code": 0, "data": {"ticket": "ticket_1", "job_status": 1}}

    def get_json(self, path: str, *, headers: dict | None = None, params: dict | None = None) -> dict:
        self.get_calls.append({"path": path, "headers": headers, "params": params})
        if self.get_payloads:
            return self.get_payloads.pop(0)
        return {
            "code": 0,
            "data": {
                "ticket": "ticket_1",
                "job_status": 0,
                "token": "cloud_token_1",
                "url": "https://feishu.example/cloud_slides",
                "type": "slides",
            },
        }


class FeishuImportAPITests(unittest.TestCase):
    def test_create_import_task_posts_payload_and_normalizes_ticket(self) -> None:
        client = DummyClient()
        api = FeishuImportAPI(auth_service=DummyAuthService(), client=client)

        result = api.create_import_task(
            file_token="import_file_token_1",
            file_extension=".pptx",
            import_type="slides",
            file_name="run_1.pptx",
            folder_token="folder_1",
        )

        self.assertEqual(result["ticket"], "ticket_1")
        self.assertEqual(result["status"], "pending")
        call = client.post_calls[0]
        self.assertEqual(call["path"], "/open-apis/drive/v1/import_tasks")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tenant-token")
        self.assertEqual(call["json"]["file_token"], "import_file_token_1")
        self.assertEqual(call["json"]["file_extension"], "pptx")
        self.assertEqual(call["json"]["type"], "slides")
        self.assertEqual(call["json"]["file_name"], "run_1.pptx")
        self.assertEqual(call["json"]["point"], {"mount_type": 1, "mount_key": "folder_1"})

    def test_wait_for_import_polls_until_ready(self) -> None:
        client = DummyClient()
        client.get_payloads = [
            {"code": 0, "data": {"ticket": "ticket_1", "job_status": 1}},
            {
                "code": 0,
                "data": {
                    "ticket": "ticket_1",
                    "job_status": 0,
                    "token": "cloud_token_1",
                    "url": "https://feishu.example/cloud_slides",
                    "type": "slides",
                },
            },
        ]
        api = FeishuImportAPI(auth_service=DummyAuthService(), client=client)

        result = api.wait_for_import("ticket_1", timeout_seconds=1, poll_seconds=0)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["token"], "cloud_token_1")
        self.assertEqual(result["url"], "https://feishu.example/cloud_slides")
        self.assertEqual(len(client.get_calls), 2)
        self.assertEqual(client.get_calls[0]["path"], "/open-apis/drive/v1/import_tasks/ticket_1")

    def test_wait_for_import_returns_failed_terminal_status(self) -> None:
        client = DummyClient()
        client.get_payloads = [{"code": 0, "data": {"ticket": "ticket_1", "job_status": 2}}]
        api = FeishuImportAPI(auth_service=DummyAuthService(), client=client)

        result = api.wait_for_import("ticket_1", timeout_seconds=1, poll_seconds=0)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["raw_status"], 2)


if __name__ == "__main__":
    unittest.main()
