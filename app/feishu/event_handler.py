import json
import re

from app.core.config import settings
from app.schemas.feishu_event import (
    FeishuEventEnvelope,
    FeishuEventType,
    FeishuMention,
    FeishuMentionedUser,
    FeishuMessageContext,
)


class FeishuEventHandler:
    """Parses incoming Feishu event payloads into an internal message context."""

    BOT_NAME_HINTS = ("机器人", "智能助手", "assistant", "bot")

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
        if not raw_text:
            return None

        parsed_mentions = self._parse_mentions(message.mentions or [])
        text = self._strip_mentions(raw_text, message.mentions or [])

        session_id = (
            message.chat_id
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
            sender_user_id=sender.sender_id.user_id,
            sender_open_id=sender.sender_id.open_id,
            sender_union_id=sender.sender_id.union_id,
            text=text,
            raw_text=raw_text,
            is_mentioned=any(user.is_bot for user in parsed_mentions),
            mentioned_users=parsed_mentions,
        )

    def _parse_message_content(self, content: str | None) -> dict:
        if not content:
            return {}

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}

        return parsed if isinstance(parsed, dict) else {"text": str(parsed)}

    def _parse_mentions(self, mentions: list[FeishuMention]) -> list[FeishuMentionedUser]:
        parsed: list[FeishuMentionedUser] = []
        for mention in mentions:
            mention_id = mention.id or None
            parsed.append(
                FeishuMentionedUser(
                    user_id=mention_id.user_id if mention_id else None,
                    open_id=mention_id.open_id if mention_id else None,
                    union_id=mention_id.union_id if mention_id else None,
                    name=mention.name,
                    key=mention.key,
                    is_bot=self._is_bot_mention(mention),
                )
            )
        return parsed

    def _is_bot_mention(self, mention: FeishuMention) -> bool:
        mention_name = (mention.name or "").strip().lower()
        mention_id = mention.id or None
        candidate_ids = {
            (settings.feishu_bot_user_id or "").strip(),
            (settings.feishu_bot_open_id or "").strip(),
        }
        mention_ids = {
            mention_id.user_id if mention_id else None,
            mention_id.open_id if mention_id else None,
        }

        if candidate_ids.intersection({value for value in mention_ids if value}):
            return True

        configured_names = {
            (settings.feishu_bot_name or "").strip().lower(),
            (settings.app_name or "").strip().lower(),
        }
        configured_names = {name for name in configured_names if name}
        if mention_name and mention_name in configured_names:
            return True

        return bool(mention_name and any(hint in mention_name for hint in self.BOT_NAME_HINTS))

    def _strip_mentions(self, text: str, mentions: list[FeishuMention]) -> str:
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
