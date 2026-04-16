from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="Feishu IM Agent MVP", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    database_url: str = Field(default="sqlite:///./data/app.db", alias="DATABASE_URL")

    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", alias="FEISHU_APP_SECRET")
    feishu_verification_token: str = Field(default="", alias="FEISHU_VERIFICATION_TOKEN")
    feishu_encrypt_key: str = Field(default="", alias="FEISHU_ENCRYPT_KEY")
    feishu_api_base_url: str = Field(default="https://open.feishu.cn", alias="FEISHU_API_BASE_URL")
    feishu_reply_enabled: bool = Field(default=False, alias="FEISHU_REPLY_ENABLED")
    feishu_bitable_enabled: bool = Field(default=False, alias="FEISHU_BITABLE_ENABLED")
    feishu_bitable_app_token: str = Field(default="", alias="FEISHU_BITABLE_APP_TOKEN")
    feishu_bitable_table_id: str = Field(default="", alias="FEISHU_BITABLE_TABLE_ID")
    feishu_bitable_title_field: str = Field(default="任务", alias="FEISHU_BITABLE_TITLE_FIELD")
    feishu_bitable_owner_field: str = Field(default="负责人", alias="FEISHU_BITABLE_OWNER_FIELD")
    feishu_bitable_due_date_field: str = Field(default="截止时间", alias="FEISHU_BITABLE_DUE_DATE_FIELD")
    feishu_bitable_priority_field: str = Field(default="优先级", alias="FEISHU_BITABLE_PRIORITY_FIELD")
    feishu_bitable_status_field: str = Field(default="状态", alias="FEISHU_BITABLE_STATUS_FIELD")
    feishu_bitable_notes_field: str = Field(default="备注", alias="FEISHU_BITABLE_NOTES_FIELD")
    feishu_bitable_session_field: str = Field(default="会话ID", alias="FEISHU_BITABLE_SESSION_FIELD")

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek/deepseek-v3.2", alias="LLM_MODEL")

    anthropic_auth_token: str = Field(default="", alias="ANTHROPIC_AUTH_TOKEN")
    anthropic_base_url: str = Field(default="", alias="ANTHROPIC_BASE_URL")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )


settings = Settings()
