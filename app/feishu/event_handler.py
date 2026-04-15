import json
import re

from app.core.config import settings
from app.schemas.feishu_event import (
    FeishuEventEnvelope,
    FeishuEventType,
    FeishuMessageContext,
)


class FeishuEventHandler:
    """Parses incoming Feishu event payloads into an internal message context."""

    def parse_event(self, payload: dict) -> FeishuEventEnvelope:
        return FeishuEventEnvelope.model_validate(payload)

    def is_url_verification(self, envelope: FeishuEventEnvelope) -> bool:
        return envelope.type == FeishuEventType.URL_VERIFICATION

    def verify_token(self, envelope: FeishuEventEnvelope) -> bool:
        if not settings.feishu_verification_token:
            return True

        candidates = [
            envelope.token,
            envelope.header.token if envelope.header else None,
        ]
        return settings.feishu_verification_token in candidates

    def extract_message_context(self, envelope: FeishuEventEnvelope) -> FeishuMessageContext | None:
        event_type = envelope.header.event_type if envelope.header else None
        if event_type != "im.message.receive_v1" or envelope.event is None:
            return None

        message = envelope.event.message
        sender = envelope.event.sender
        if message is None or sender is None:
            return None

        parsed_content = self._parse_message_content(message.content)
        raw_text = parsed_content.get("text", "").strip()
        mentions = message.mentions or []
        text = self._strip_mentions(raw_text, mentions)
        if not raw_text:
            return None

        session_id = (
            message.chat_id
            or message.chat_id
            or sender.sender_id.open_id
            or sender.sender_id.user_id
            or message.message_id
        )

        sender_label = (
            sender.sender_id.user_id
            or sender.sender_id.open_id
            or sender.sender_id.union_id
            or "unknown-user"
        )

        return FeishuMessageContext(
            event_id=envelope.header.event_id if envelope.header else None,
            event_type=event_type,
            message_id=message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            session_id=session_id,
            sender_id=sender_label,
            text=text,
            raw_text=raw_text,
            is_mentioned=bool(mentions),
        )

    def _parse_message_content(self, content: str | None) -> dict:
        if not content:
            return {}

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}

        return parsed if isinstance(parsed, dict) else {"text": str(parsed)}

    def _strip_mentions(self, text: str, mentions: list) -> str:
        cleaned = text
        for mention in mentions:
            key = getattr(mention, "key", None)
            name = getattr(mention, "name", None)
            if key:
                cleaned = cleaned.replace(key, " ")
            if name:
                cleaned = cleaned.replace(f"@{name}", " ")

        cleaned = re.sub(r"@[^\s]+\s*", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
