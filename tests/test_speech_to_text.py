import base64
import unittest
from unittest.mock import patch

from app.services import speech_to_text
from app.services.speech_to_text import SpeechToTextService


class FakeResponse:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> None:
        self.headers = headers or {
            "X-Api-Status-Code": "20000000",
            "X-Api-Message": "OK",
            "X-Tt-Logid": "log-1",
        }
        self._payload = payload or {"result": {"text": "hello world"}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse, capture: dict) -> None:
        self.response = response
        self.capture = capture

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.capture["url"] = url
        self.capture["headers"] = headers
        self.capture["json"] = json
        return self.response


class SpeechToTextServiceTests(unittest.TestCase):
    def _patch_volcengine_settings(self):
        return [
            patch.object(speech_to_text.settings, "speech_to_text_provider", "volcengine"),
            patch.object(speech_to_text.settings, "volcengine_asr_enabled", True),
            patch.object(speech_to_text.settings, "volcengine_asr_api_key", "api-key"),
            patch.object(speech_to_text.settings, "volcengine_asr_app_key", ""),
            patch.object(speech_to_text.settings, "volcengine_asr_access_key", ""),
            patch.object(
                speech_to_text.settings,
                "volcengine_asr_base_url",
                "https://openspeech.bytedance.com",
            ),
            patch.object(
                speech_to_text.settings,
                "volcengine_asr_resource_id",
                "volc.bigasr.auc_turbo",
            ),
            patch.object(speech_to_text.settings, "volcengine_asr_model_name", "bigmodel"),
        ]

    def test_volcengine_provider_is_configured_with_new_api_key(self) -> None:
        patches = self._patch_volcengine_settings()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        service = SpeechToTextService()

        self.assertTrue(service.is_configured())

    def test_transcribe_bytes_uses_volcengine_flash_api(self) -> None:
        patches = self._patch_volcengine_settings()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        capture: dict = {}
        response = FakeResponse()

        def client_factory(*, timeout):
            return FakeClient(response, capture)

        with patch.object(speech_to_text.httpx, "Client", side_effect=client_factory):
            text = SpeechToTextService().transcribe_bytes(
                content=b"voice-bytes",
                content_type="audio/ogg",
            )

        self.assertEqual(text, "hello world")
        self.assertEqual(
            capture["url"],
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        )
        self.assertEqual(capture["headers"]["X-Api-Key"], "api-key")
        self.assertEqual(capture["headers"]["X-Api-Resource-Id"], "volc.bigasr.auc_turbo")
        self.assertEqual(capture["headers"]["X-Api-Sequence"], "-1")
        self.assertEqual(capture["json"]["user"], {"uid": "api-key"})
        self.assertEqual(
            capture["json"]["audio"],
            {"data": base64.b64encode(b"voice-bytes").decode("ascii")},
        )
        self.assertEqual(capture["json"]["request"], {"model_name": "bigmodel"})

    def test_volcengine_error_header_raises(self) -> None:
        patches = self._patch_volcengine_settings()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        response = FakeResponse(
            headers={
                "X-Api-Status-Code": "45000151",
                "X-Api-Message": "invalid audio format",
                "X-Tt-Logid": "log-bad",
            }
        )

        def client_factory(*, timeout):
            return FakeClient(response, {})

        with patch.object(speech_to_text.httpx, "Client", side_effect=client_factory):
            with self.assertRaisesRegex(RuntimeError, "45000151"):
                SpeechToTextService().transcribe_bytes(content=b"bad-audio")


if __name__ == "__main__":
    unittest.main()
