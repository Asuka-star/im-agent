import unittest
from unittest.mock import patch

from app.feishu.message_api import FeishuMessageAPI
from app.services.feishu_card_builder import FeishuArtifactCardBuilder


class _FakeAuth:
    def get_tenant_access_token(self) -> str:
        return "tenant_token"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post_json(self, path: str, *, json: dict, headers: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append({"path": path, "json": json, "headers": headers, "params": params})
        return {"code": 0, "data": {"message_id": "om_1"}}


class FeishuArtifactCardBuilderTests(unittest.TestCase):
    def test_builds_interactive_card_with_artifact_buttons(self) -> None:
        with patch("app.services.feishu_card_builder.settings.artifact_public_base_url", "https://demo.example"):
            card = FeishuArtifactCardBuilder().build_artifact_card(
                title="交付完成",
                mode="slides",
                summary="已经生成 PPT 和白板",
                artifacts=[
                    {
                        "artifact_type": "slides_package",
                        "title": "汇报 PPT",
                        "url": "/api/artifacts/slides/run.html",
                        "preview": {
                            "slides": [
                                {"title": "开场", "speaker_notes": "介绍价值"},
                                {"title": "方案"},
                            ]
                        },
                    },
                    {
                        "artifact_type": "canvas",
                        "title": "流程图",
                        "url": "/api/artifacts/canvas/run.html",
                        "preview": {"summary": {"node_count": 5, "arrow_count": 4}},
                    },
                ],
            )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card["header"]["title"]["content"], "交付完成")
        action = next(item for item in card["elements"] if item.get("tag") == "action")
        self.assertEqual(action["actions"][0]["url"], "https://demo.example/api/artifacts/slides/run.html")
        self.assertEqual(action["actions"][0]["text"]["content"], "打开演示稿")

    def test_returns_none_without_openable_artifacts(self) -> None:
        card = FeishuArtifactCardBuilder().build_artifact_card(
            title="无链接",
            mode="doc",
            artifacts=[{"artifact_type": "document", "title": "本地草稿"}],
        )

        self.assertIsNone(card)


class FeishuMessageAPITests(unittest.TestCase):
    def test_send_interactive_message_uses_feishu_interactive_payload(self) -> None:
        client = _FakeClient()
        api = FeishuMessageAPI(auth_service=_FakeAuth(), client=client)

        result = api.send_interactive_message("chat_1", {"elements": []})

        self.assertEqual(result["code"], 0)
        self.assertEqual(client.calls[0]["path"], "/open-apis/im/v1/messages")
        self.assertEqual(client.calls[0]["params"], {"receive_id_type": "chat_id"})
        self.assertEqual(client.calls[0]["json"]["msg_type"], "interactive")
        self.assertIn("elements", client.calls[0]["json"]["content"])


if __name__ == "__main__":
    unittest.main()
