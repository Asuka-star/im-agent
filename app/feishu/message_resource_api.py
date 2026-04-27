from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient


class FeishuMessageResourceAPI:
    """Downloads resource files attached to Feishu IM messages."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
    ) -> tuple[bytes, str | None]:
        access_token = self.auth_service.get_tenant_access_token()
        response = self.client.get_response(
            f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"type": resource_type},
        )
        return response.content, response.headers.get("content-type")
