from pydantic import BaseModel


class FeishuMessageEvent(BaseModel):
    event_id: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    text: str | None = None
