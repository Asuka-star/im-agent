from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuUserAPI:
    """Wraps Feishu user-profile lookup for readable sender names."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def get_user_display_name(
        self,
        *,
        user_id: str | None = None,
        open_id: str | None = None,
    ) -> str | None:
        identifier = (user_id or open_id or "").strip()
        if not identifier:
            return None

        id_type = "user_id" if user_id else "open_id"
        access_token = self.auth_service.get_tenant_access_token()
        data = self.client.get_json(
            f"/open-apis/contact/v3/users/{identifier}",
            params={"user_id_type": id_type},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user = data.get("data", {}).get("user", {})
        name = str(user.get("name") or "").strip()
        return name or None
