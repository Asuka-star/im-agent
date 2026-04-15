from enum import StrEnum

from pydantic import BaseModel, Field


class FeishuEventType(StrEnum):
    URL_VERIFICATION = "url_verification"
    EVENT_CALLBACK = "event_callback"


class FeishuEventHeader(BaseModel):
    event_id: str | None = None
    event_type: str | None = None
    tenant_key: str | None = None
    app_id: str | None = None
    create_time: str | None = None
    token: str | None = None


class FeishuSenderId(BaseModel):
    union_id: str | None = None
    user_id: str | None = None
    open_id: str | None = None


class FeishuSender(BaseModel):
    sender_id: FeishuSenderId = FeishuSenderId()
    sender_type: str | None = None
    tenant_key: str | None = None


class FeishuMentionId(BaseModel):
    union_id: str | None = None
    user_id: str | None = None
    open_id: str | None = None


class FeishuMention(BaseModel):
    key: str | None = None
    id: FeishuMentionId | None = None
    name: str | None = None
    tenant_key: str | None = None


class FeishuMessage(BaseModel):
    message_id: str | None = None
    root_id: str | None = None
    parent_id: str | None = None
    chat_id: str | None = None
    chat_type: str | None = None
    message_type: str | None = None
    content: str | None = None
    create_time: str | None = None
    mentions: list[FeishuMention] = []


class FeishuMessageEvent(BaseModel):
    sender: FeishuSender | None = None
    message: FeishuMessage | None = None


class FeishuEventEnvelope(BaseModel):
    type: str | None = None
    challenge: str | None = None
    token: str | None = None
    schema_version: str | None = Field(default=None, alias="schema")
    header: FeishuEventHeader | None = None
    event: FeishuMessageEvent | None = None


class FeishuMessageContext(BaseModel):
    event_id: str | None = None
    event_type: str | None = None
    message_id: str | None = None
    chat_id: str | None = None
    chat_type: str | None = None
    session_id: str
    sender_id: str
    text: str
    raw_text: str
    is_mentioned: bool = False
