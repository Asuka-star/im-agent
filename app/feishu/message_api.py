import json

from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuMessageAPI:
    """Wraps the Feishu send-message API."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def send_text_message(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id") -> dict:
        access_token = self.auth_service.get_tenant_access_token()
        return self.client.post_json(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def send_interactive_message(self, receive_id: str, card: dict, *, receive_id_type: str = "chat_id") -> dict:
        access_token = self.auth_service.get_tenant_access_token()
        return self.client.post_json(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )

    def update_message(self, message_id: str, content: dict, *, msg_type: str = "interactive") -> dict:
        access_token = self.auth_service.get_tenant_access_token()
        return self.client.patch_json(
            f"/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )

    def patch_card(self, message_id: str, card: dict) -> dict:
        return self.update_message(message_id, card, msg_type="interactive")
