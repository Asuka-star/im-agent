import unittest

from app.feishu.event_handler import FeishuEventHandler


class DummyMessageResourceAPI:
    def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
    ) -> tuple[bytes, str | None]:
        return b"voice-bytes", "audio/ogg"


class DummySpeechToTextService:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    def is_configured(self) -> bool:
        return True

    def unavailable_notice(self) -> str:
        return "当前没有启用语音识别，请直接发送文本消息。"

    def failure_notice(self) -> str:
        return "这条语音消息处理失败了，请直接发送文本消息。"

    def transcribe_bytes(
        self,
        *,
        content: bytes,
        content_type: str | None = None,
    ) -> str:
        return self.transcript


class EventHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = FeishuEventHandler()

    def test_mentioning_other_user_does_not_trigger_bot_mode(self) -> None:
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-1"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-1",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "content": "{\"text\":\"@张三 你来搞后端\"}",
                    "mentions": [
                        {"key": "@_user_1", "name": "张三", "id": {"user_id": "user-zhangsan"}}
                    ],
                },
            },
        }

        context = self.handler.extract_message_context(self.handler.parse_event(payload))
        assert context is not None
        self.assertFalse(context.is_mentioned)
        self.assertEqual(context.mentioned_user_names, ["张三"])

    def test_mentioning_bot_marks_request_as_trigger(self) -> None:
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-2"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-2",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "content": "{\"text\":\"@机器人 @张三 你来搞后端\"}",
                    "mentions": [
                        {"key": "@_user_1", "name": "机器人", "id": {"user_id": "bot-1"}},
                        {"key": "@_user_2", "name": "张三", "id": {"user_id": "user-zhangsan"}},
                    ],
                },
            },
        }

        context = self.handler.extract_message_context(self.handler.parse_event(payload))
        assert context is not None
        self.assertTrue(context.is_mentioned)
        self.assertEqual(context.mentioned_user_names, ["张三"])

    def test_audio_message_is_transcribed_into_context(self) -> None:
        handler = FeishuEventHandler(
            message_resource_api=DummyMessageResourceAPI(),
            speech_to_text_service=DummySpeechToTextService("请帮我整理一下当前待办"),
        )
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-3"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-3",
                    "chat_id": "chat-1",
                    "chat_type": "p2p",
                    "message_type": "audio",
                    "content": "{\"file_key\":\"file-audio-1\",\"duration\":1800}",
                },
            },
        }

        context = handler.extract_message_context(handler.parse_event(payload))
        assert context is not None
        self.assertEqual(context.message_type, "audio")
        self.assertEqual(context.file_key, "file-audio-1")
        self.assertEqual(context.text, "请帮我整理一下当前待办")
        self.assertEqual(context.raw_text, "请帮我整理一下当前待办")
        self.assertFalse(context.is_mentioned)

    def test_audio_message_with_bot_name_prefix_triggers_bot_mode(self) -> None:
        handler = FeishuEventHandler(
            message_resource_api=DummyMessageResourceAPI(),
            speech_to_text_service=DummySpeechToTextService("机器人，帮我总结一下这轮讨论"),
        )
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-4"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-4",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "message_type": "audio",
                    "content": "{\"file_key\":\"file-audio-2\",\"duration\":2200}",
                },
            },
        }

        context = handler.extract_message_context(handler.parse_event(payload))
        assert context is not None
        self.assertTrue(context.is_mentioned)
        self.assertEqual(context.text, "帮我总结一下这轮讨论")
        self.assertEqual(context.raw_text, "机器人，帮我总结一下这轮讨论")

    def test_audio_message_without_transcriber_returns_notice(self) -> None:
        class DisabledSpeechToTextService:
            def is_configured(self) -> bool:
                return False

            def unavailable_notice(self) -> str:
                return "当前没有启用语音识别，请直接发送文本消息。"

            def failure_notice(self) -> str:
                return "这条语音消息处理失败了，请直接发送文本消息。"

        handler = FeishuEventHandler(
            message_resource_api=DummyMessageResourceAPI(),
            speech_to_text_service=DisabledSpeechToTextService(),
        )
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-5"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-5",
                    "chat_id": "chat-1",
                    "chat_type": "p2p",
                    "message_type": "audio",
                    "content": "{\"file_key\":\"file-audio-3\",\"duration\":2200}",
                },
            },
        }

        context = handler.extract_message_context(handler.parse_event(payload))
        assert context is not None
        self.assertEqual(context.file_key, "file-audio-3")
        self.assertEqual(context.transcription_notice, "当前没有启用语音识别，请直接发送文本消息。")
        self.assertEqual(context.text, "")


if __name__ == "__main__":
    unittest.main()
