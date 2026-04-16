from app.core.config import settings
from app.feishu.auth import FeishuAuthService
from app.feishu.client import FeishuClient
from app.schemas.task import TaskItem


class FeishuBitableAPI:
    """Wraps Feishu Bitable record creation for task sync."""

    def __init__(
        self,
        auth_service: FeishuAuthService | None = None,
        client: FeishuClient | None = None,
    ) -> None:
        self.client = client or FeishuClient(base_url=settings.feishu_api_base_url)
        self.auth_service = auth_service or FeishuAuthService(client=self.client)

    def is_configured(self) -> bool:
        return bool(
            settings.feishu_bitable_enabled
            and settings.feishu_bitable_app_token
            and settings.feishu_bitable_table_id
        )

    def create_record(self, fields: dict) -> dict:
        if not self.is_configured():
            raise RuntimeError("Feishu bitable sync is not configured.")

        access_token = self.auth_service.get_tenant_access_token()
        path = (
            f"/open-apis/bitable/v1/apps/{settings.feishu_bitable_app_token}"
            f"/tables/{settings.feishu_bitable_table_id}/records"
        )
        return self.client.post_json(
            path,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"fields": fields},
        )

    def create_task_record(self, task: TaskItem, *, session_id: str) -> dict:
        fields = {
            settings.feishu_bitable_title_field: task.title,
            settings.feishu_bitable_owner_field: task.owner,
            settings.feishu_bitable_due_date_field: task.due_date,
            settings.feishu_bitable_priority_field: task.priority,
            settings.feishu_bitable_status_field: task.status,
            settings.feishu_bitable_notes_field: task.notes,
            settings.feishu_bitable_session_field: session_id,
        }
        return self.create_record(fields)
