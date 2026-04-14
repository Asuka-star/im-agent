from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.feishu.client import FeishuClient


class FeishuAuthService:
    """Fetches and caches the tenant access token for a Feishu self-built app."""

    def __init__(self, client: FeishuClient | None = None) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._is_valid():
            return self._token or ""

        if not settings.feishu_app_id or not settings.feishu_app_secret:
            raise RuntimeError("Feishu app credentials are not configured.")

        data = self.client.post_json(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        token = data.get("tenant_access_token")
        expire_seconds = int(data.get("expire", 0))
        if not token:
            raise RuntimeError("Feishu auth response did not include tenant_access_token.")

        self._token = token
        ttl = max(expire_seconds - 60, 60)
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        return token

    def _is_valid(self) -> bool:
        return bool(self._token and self._expires_at and datetime.now(timezone.utc) < self._expires_at)
