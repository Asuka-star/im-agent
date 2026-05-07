import json
import logging
import re

from app.core.config import settings
from app.feishu.message_resource_api import FeishuMessageResourceAPI
from app.schemas.feishu_event import (
    FeishuEventEnvelope,
    FeishuEventType,
    FeishuMention,
    FeishuMentionedUser,
    FeishuMessage,
    FeishuMessageContext,
    FeishuMessageLifecycleContext,
)
from app.services.speech_to_text import SpeechToTextService


logger = logging.getLogger(__name__)


class FeishuEventHandler:
    """Parses incoming Feishu event payloads into an internal message context."""

    BOT_NAME_HINTS = ("机器人", "智能助手", "assistant", "bot")
    MESSAGE_RECALLED_EVENT = "im.message.recalled_v1"
    MESSAGE_UPDATED_EVENTS = {"im.message.updated_v1", "im.message.message_updated_v1"}

    def __init__(
        self,
        *,
        message_resource_api: FeishuMessageResourceAPI | None = None,
        speech_to_text_service: SpeechToTextService | None = None,
    ) -> None:
        self.message_resource_api = message_resource_api or FeishuMessageResourceAPI()
        self.speech_to_text_service = speech_to_text_service or SpeechToTextService()

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

    def extract_message_context(
        self, envelope: FeishuEventEnvelope
    ) -> FeishuMessageContext | None:
        event_type = envelope.header.event_type if envelope.header else None
        if event_type != "im.message.receive_v1" or envelope.event is None:
            return None

        message = envelope.event.message
        sender = envelope.event.sender
        if message is None or sender is None:
            return None

        parsed_content = self._parse_message_content(message.content)
        raw_text, file_key, file_name, transcription_notice = self._extract_message_text(
            message=message,
            parsed_content=parsed_content,
        )
        if not raw_text and not transcription_notice and not file_key:
            return None

        parsed_mentions = self._parse_mentions(message.mentions or [])
        text = self._strip_mentions(raw_text, message.mentions or [])
        voice_mention = False
        text_prefix_mention = False
        if (message.message_type or "").strip().lower() == "audio":
            text, voice_mention = self._strip_voice_bot_prefix(text)
        else:
            stripped_text, text_prefix_mention = self._strip_voice_bot_prefix(raw_text)
            if text_prefix_mention:
                text = self._strip_mentions(stripped_text, message.mentions or [])

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
            tenant_key=(
                envelope.header.tenant_key
                if envelope.header and envelope.header.tenant_key
                else sender.tenant_key
            ),
            message_id=message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            message_type=message.message_type,
            session_id=session_id,
            sender_id=sender_label,
            sender_user_id=sender.sender_id.user_id,
            sender_open_id=sender.sender_id.open_id,
            sender_union_id=sender.sender_id.union_id,
            text=text,
            raw_text=raw_text,
            file_key=file_key,
            file_name=file_name,
            transcription_notice=transcription_notice,
            is_mentioned=any(user.is_bot for user in parsed_mentions) or voice_mention or text_prefix_mention,
            mentioned_users=parsed_mentions,
        )

    def is_message_lifecycle_event(self, envelope: FeishuEventEnvelope) -> bool:
        event_type = (envelope.header.event_type if envelope.header else "") or ""
        return event_type in {self.MESSAGE_RECALLED_EVENT, *self.MESSAGE_UPDATED_EVENTS}

    def extract_message_lifecycle_context(self, payload: dict) -> FeishuMessageLifecycleContext | None:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        event_type = str(header.get("event_type") or payload.get("type") or "").strip()
        if event_type not in {self.MESSAGE_RECALLED_EVENT, *self.MESSAGE_UPDATED_EVENTS}:
            return None

        message_payload = event.get("message") if isinstance(event.get("message"), dict) else event
        message_id = str(message_payload.get("message_id") or event.get("message_id") or "").strip()
        if not message_id:
            return None

        chat_id = str(message_payload.get("chat_id") or event.get("chat_id") or "").strip() or None
        parsed_content = self._parse_message_content(
            message_payload.get("content") if isinstance(message_payload, dict) else None
        )
        raw_text = str(parsed_content.get("text") or "").strip()

        return FeishuMessageLifecycleContext(
            event_id=header.get("event_id"),
            event_type=event_type,
            tenant_key=header.get("tenant_key"),
            message_id=message_id,
            chat_id=chat_id,
            session_id=chat_id or message_id,
            message_type=message_payload.get("message_type") if isinstance(message_payload, dict) else None,
            text=raw_text or None,
            raw_text=raw_text or None,
            recall_time=str(event.get("recall_time") or "").strip() or None,
            recall_type=str(event.get("recall_type") or "").strip() or None,
        )

    def _parse_message_content(self, content: str | None) -> dict:
        if not content:
            return {}

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}

        return parsed if isinstance(parsed, dict) else {"text": str(parsed)}

    def _extract_message_text(
        self,
        *,
        message: FeishuMessage,
        parsed_content: dict,
    ) -> tuple[str, str | None, str | None, str | None]:
        message_type = (message.message_type or "text").strip().lower()
        if message_type in {"", "text", "post"}:
            return str(parsed_content.get("text") or "").strip(), None, None, None

        if message_type == "audio":
            return self._extract_audio_text(
                message=message, parsed_content=parsed_content
            )

        if message_type == "file":
            return self._extract_file_text(parsed_content=parsed_content)

        return "", None, None, None

    def _extract_audio_text(
        self,
        *,
        message: FeishuMessage,
        parsed_content: dict,
    ) -> tuple[str, str | None, str | None, str | None]:
        file_key = str(parsed_content.get("file_key") or "").strip()
        if not file_key:
            logger.info(
                "Ignoring audio message without file_key: message_id=%s",
                message.message_id,
            )
            return "", None, None, None

        if not self.speech_to_text_service.is_configured():
            notice = self.speech_to_text_service.unavailable_notice()
            logger.warning(
                "Audio message cannot be transcribed yet: message_id=%s reason=%s",
                message.message_id,
                notice,
            )
            return "", file_key, None, notice

        if not message.message_id:
            logger.warning(
                "Ignoring audio message without message_id: file_key=%s", file_key
            )
            return "", file_key, None, self.speech_to_text_service.failure_notice()

        try:
            audio_bytes, content_type = (
                self.message_resource_api.download_message_resource(
                    message_id=message.message_id,
                    file_key=file_key,
                    resource_type="file",
                )
            )
            transcript = self.speech_to_text_service.transcribe_bytes(
                content=audio_bytes,
                content_type=content_type,
            )
            return transcript.strip(), file_key, None, None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Audio transcription failed: message_id=%s error=%s",
                message.message_id,
                exc,
            )
            return "", file_key, None, self.speech_to_text_service.failure_notice()

    def _extract_file_text(
        self,
        *,
        parsed_content: dict,
    ) -> tuple[str, str | None, str | None, str | None]:
        file_key = str(parsed_content.get("file_key") or "").strip()
        file_name = str(
            parsed_content.get("file_name")
            or parsed_content.get("name")
            or parsed_content.get("title")
            or ""
        ).strip()
        text = str(parsed_content.get("text") or "").strip() or file_name
        if not file_key:
            return "", None, file_name or None, None
        return text, file_key, file_name or None, None

    def _parse_mentions(
        self, mentions: list[FeishuMention]
    ) -> list[FeishuMentionedUser]:
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

        return bool(
            mention_name and any(hint in mention_name for hint in self.BOT_NAME_HINTS)
        )

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

    def _strip_voice_bot_prefix(self, text: str) -> tuple[str, bool]:
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        if not normalized:
            return "", False

        prefixes = []
        configured_names = [
            (settings.feishu_bot_name or "").strip(),
            (settings.app_name or "").strip(),
        ]
        prefixes.extend(name for name in configured_names if name)
        prefixes.extend(self.BOT_NAME_HINTS)

        for prefix in prefixes:
            pattern = rf"^\s*@?{re.escape(prefix)}[\uFF0C,\uFF1A:\s]*"
            if re.match(pattern, normalized, flags=re.IGNORECASE):
                stripped = re.sub(
                    pattern, "", normalized, count=1, flags=re.IGNORECASE
                ).strip()
                return stripped or normalized, True

        return normalized, False
