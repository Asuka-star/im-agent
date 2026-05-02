from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="im-agent", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    artifact_public_base_url: str = Field(
        default="http://science.topviewclub.cn",
        alias="ARTIFACT_PUBLIC_BASE_URL",
    )

    database_url: str = Field(default="sqlite:///./data/app.db", alias="DATABASE_URL")

    feishu_app_id: str = Field(default="", alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(default="", alias="FEISHU_APP_SECRET")
    feishu_verification_token: str = Field(default="", alias="FEISHU_VERIFICATION_TOKEN")
    feishu_encrypt_key: str = Field(default="", alias="FEISHU_ENCRYPT_KEY")
    feishu_api_base_url: str = Field(default="https://open.feishu.cn", alias="FEISHU_API_BASE_URL")
    feishu_reply_enabled: bool = Field(default=False, alias="FEISHU_REPLY_ENABLED")
    feishu_reply_card_enabled: bool = Field(default=False, alias="FEISHU_REPLY_CARD_ENABLED")
    feishu_bot_name: str = Field(default="", alias="FEISHU_BOT_NAME")
    feishu_bot_user_id: str = Field(default="", alias="FEISHU_BOT_USER_ID")
    feishu_bot_open_id: str = Field(default="", alias="FEISHU_BOT_OPEN_ID")
    feishu_doc_enabled: bool = Field(default=False, alias="FEISHU_DOC_ENABLED")
    feishu_doc_folder_token: str = Field(default="", alias="FEISHU_DOC_FOLDER_TOKEN")
    feishu_doc_title_prefix: str = Field(default="协同产出", alias="FEISHU_DOC_TITLE_PREFIX")
    feishu_doc_auto_folder_name: str = Field(default="AI协作产出", alias="FEISHU_DOC_AUTO_FOLDER_NAME")

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

    speech_to_text_provider: str = Field(default="deepgram", alias="SPEECH_TO_TEXT_PROVIDER")

    deepgram_enabled: bool = Field(default=False, alias="DEEPGRAM_ENABLED")
    deepgram_api_key: str = Field(default="", alias="DEEPGRAM_API_KEY")
    deepgram_base_url: str = Field(default="https://api.deepgram.com/v1", alias="DEEPGRAM_BASE_URL")
    deepgram_model: str = Field(default="nova-3", alias="DEEPGRAM_MODEL")
    deepgram_language: str = Field(default="zh-CN", alias="DEEPGRAM_LANGUAGE")

    volcengine_asr_enabled: bool = Field(default=False, alias="VOLCENGINE_ASR_ENABLED")
    volcengine_asr_api_key: str = Field(default="", alias="VOLCENGINE_ASR_API_KEY")
    volcengine_asr_app_key: str = Field(default="", alias="VOLCENGINE_ASR_APP_KEY")
    volcengine_asr_access_key: str = Field(default="", alias="VOLCENGINE_ASR_ACCESS_KEY")
    volcengine_asr_base_url: str = Field(
        default="https://openspeech.bytedance.com",
        alias="VOLCENGINE_ASR_BASE_URL",
    )
    volcengine_asr_resource_id: str = Field(
        default="volc.bigasr.auc_turbo",
        alias="VOLCENGINE_ASR_RESOURCE_ID",
    )
    volcengine_asr_model_name: str = Field(default="bigmodel", alias="VOLCENGINE_ASR_MODEL_NAME")

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek/deepseek-v3.2", alias="LLM_MODEL")
    llm_timeout_seconds: float = Field(default=45.0, alias="LLM_TIMEOUT_SECONDS")
    llm_memory_gate_timeout_seconds: float = Field(default=12.0, alias="LLM_MEMORY_GATE_TIMEOUT_SECONDS")

    embedding_api_key: str = Field(default="", alias="EMBEDDING_API_KEY")
    embedding_base_url: str = Field(default="", alias="EMBEDDING_BASE_URL")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=1024, alias="EMBEDDING_DIMENSIONS")
    memory_message_chunk_keep: int = Field(default=120, alias="MEMORY_MESSAGE_CHUNK_KEEP")
    memory_assistant_chunk_keep: int = Field(default=80, alias="MEMORY_ASSISTANT_CHUNK_KEEP")
    memory_summary_chunk_keep: int = Field(default=200, alias="MEMORY_SUMMARY_CHUNK_KEEP")

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
