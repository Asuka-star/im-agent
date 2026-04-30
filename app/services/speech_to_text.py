import base64
import logging
from typing import Any
import uuid

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class SpeechToTextService:
    """Speech-to-text wrapper for inbound voice messages."""

    def __init__(self) -> None:
        self.provider = (settings.speech_to_text_provider or "deepgram").strip().lower()
        self.api_key = settings.deepgram_api_key
        self.base_url = (settings.deepgram_base_url or "").rstrip("/")
        self.model = settings.deepgram_model
        self.language = (settings.deepgram_language or "").strip()
        self.volcengine_api_key = settings.volcengine_asr_api_key
        self.volcengine_app_key = settings.volcengine_asr_app_key
        self.volcengine_access_key = settings.volcengine_asr_access_key
        self.volcengine_base_url = (settings.volcengine_asr_base_url or "").rstrip("/")
        self.volcengine_resource_id = settings.volcengine_asr_resource_id
        self.volcengine_model_name = settings.volcengine_asr_model_name

    def is_configured(self) -> bool:
        if self.provider == "volcengine":
            return bool(
                settings.volcengine_asr_enabled
                and self.volcengine_base_url
                and self.volcengine_resource_id
                and (
                    self.volcengine_api_key
                    or (self.volcengine_app_key and self.volcengine_access_key)
                )
            )

        return bool(
            settings.deepgram_enabled
            and self.api_key
            and self.base_url
            and self.model
        )

    def unavailable_notice(self) -> str:
        if self.provider == "volcengine":
            if not settings.volcengine_asr_enabled:
                return "当前没有启用语音识别，请直接发送文本消息。"
            if not (
                self.volcengine_api_key
                or (self.volcengine_app_key and self.volcengine_access_key)
            ):
                return "当前语音识别配置不完整，请直接发送文本消息。"
            return "当前语音识别暂时不可用，请直接发送文本消息。"

        if not settings.deepgram_enabled:
            return "当前没有启用语音识别，请直接发送文本消息。"
        if not self.api_key:
            return "当前语音识别配置不完整，请直接发送文本消息。"
        return "当前语音识别暂时不可用，请直接发送文本消息。"

    def failure_notice(self) -> str:
        return "这条语音消息处理失败了，请直接发送文本消息。"

    def transcribe_bytes(
        self,
        *,
        content: bytes,
        content_type: str | None = None,
    ) -> str:
        if not self.is_configured():
            raise RuntimeError(self.unavailable_notice())
        if self.provider == "volcengine":
            return self._transcribe_with_volcengine(content=content)

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

    def _transcribe_with_volcengine(self, *, content: bytes) -> str:
        headers = {
            "X-Api-Resource-Id": self.volcengine_resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        if self.volcengine_api_key:
            headers["X-Api-Key"] = self.volcengine_api_key
        else:
            headers["X-Api-App-Key"] = self.volcengine_app_key
            headers["X-Api-Access-Key"] = self.volcengine_access_key

        body = {
            "user": {"uid": self.volcengine_api_key or self.volcengine_app_key},
            "audio": {"data": base64.b64encode(content).decode("ascii")},
            "request": {"model_name": self.volcengine_model_name},
        }
        timeout = httpx.Timeout(120.0, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.volcengine_base_url}/api/v3/auc/bigmodel/recognize/flash",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            status_code = response.headers.get("X-Api-Status-Code")
            if status_code != "20000000":
                message = response.headers.get("X-Api-Message", "")
                logid = response.headers.get("X-Tt-Logid", "")
                raise RuntimeError(
                    f"Volcengine ASR failed: status={status_code} message={message} logid={logid}"
                )
            payload = response.json()

        text = self._extract_volcengine_text(payload)
        logger.info(
            "Volcengine transcription succeeded: chars=%s logid=%s",
            len(text),
            response.headers.get("X-Tt-Logid", ""),
        )
        return text

    def _extract_volcengine_text(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("Volcengine response was not a JSON object.")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Volcengine response did not include result.")

        transcript = str(result.get("text") or "").strip()
        if not transcript:
            raise RuntimeError("Volcengine returned empty text.")
        return transcript

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
