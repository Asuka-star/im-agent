from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuChatAPI:
    """Wraps Feishu chat lookup for readable group names."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def get_chat_name(self, chat_id: str) -> str | None:
        normalized = (chat_id or "").strip()
        if not normalized:
            return None

        access_token = self.auth_service.get_tenant_access_token()
        data = self.client.get_json(
            f"/open-apis/im/v1/chats/{normalized}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = data.get("data", {})
        chat = payload.get("chat", payload)
        if isinstance(chat, dict):
            name = str(chat.get("name") or "").strip()
            if name:
                return name
        items = payload.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    return name
        return None
