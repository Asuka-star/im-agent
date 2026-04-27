import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class SpeechToTextService:
    """Deepgram wrapper for inbound voice messages."""

    def __init__(self) -> None:
        self.api_key = settings.deepgram_api_key
        self.base_url = (settings.deepgram_base_url or "").rstrip("/")
        self.model = settings.deepgram_model
        self.language = (settings.deepgram_language or "").strip()

    def is_configured(self) -> bool:
        return bool(
            settings.deepgram_enabled
            and self.api_key
            and self.base_url
            and self.model
        )

    def unavailable_notice(self) -> str:
        if not settings.deepgram_enabled:
            return "\u5f53\u524d\u6ca1\u6709\u542f\u7528\u8bed\u97f3\u8bc6\u522b\uff0c\u8bf7\u76f4\u63a5\u53d1\u9001\u6587\u672c\u6d88\u606f\u3002"
        if not self.api_key:
            return "\u5f53\u524d\u8bed\u97f3\u8bc6\u522b\u914d\u7f6e\u4e0d\u5b8c\u6574\uff0c\u8bf7\u76f4\u63a5\u53d1\u9001\u6587\u672c\u6d88\u606f\u3002"
        return "\u5f53\u524d\u8bed\u97f3\u8bc6\u522b\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u8bf7\u76f4\u63a5\u53d1\u9001\u6587\u672c\u6d88\u606f\u3002"

    def failure_notice(self) -> str:
        return "\u8fd9\u6761\u8bed\u97f3\u6d88\u606f\u5904\u7406\u5931\u8d25\u4e86\uff0c\u8bf7\u76f4\u63a5\u53d1\u9001\u6587\u672c\u6d88\u606f\u3002"

    def transcribe_bytes(
        self,
        *,
        content: bytes,
        content_type: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise RuntimeError(self.unavailable_notice())

        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": self._normalize_content_type(content_type),
        }
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
        }
        if self.language:
            params["language"] = self.language
        timeout = httpx.Timeout(90.0, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.base_url}/listen",
                headers=headers,
                params=params,
                content=content,
            )
            response.raise_for_status()
            payload = response.json()

        text = self._extract_text(payload)
        logger.info(
            "Deepgram transcription succeeded: content_type=%s language=%s chars=%s",
            content_type,
            self.language or "default",
            len(text),
        )
        return text

    def _extract_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("Deepgram response was not a JSON object.")

        results = payload.get("results")
        if not isinstance(results, dict):
            raise RuntimeError("Deepgram response did not include results.")

        channels = results.get("channels")
        if not isinstance(channels, list) or not channels:
            raise RuntimeError("Deepgram response did not include channels.")

        first_channel = channels[0]
        if not isinstance(first_channel, dict):
            raise RuntimeError("Deepgram response channel was invalid.")

        alternatives = first_channel.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            raise RuntimeError("Deepgram response did not include alternatives.")

        first_alternative = alternatives[0]
        if not isinstance(first_alternative, dict):
            raise RuntimeError("Deepgram transcription alternative was invalid.")

        transcript = str(first_alternative.get("transcript") or "").strip()
        if not transcript:
            raise RuntimeError("Deepgram returned empty text.")
        return transcript

    def _normalize_content_type(self, content_type: str | None) -> str:
        normalized = (content_type or "").split(";")[0].strip().lower()
        if normalized:
            return normalized
        return "application/octet-stream"
