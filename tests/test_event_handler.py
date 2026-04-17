import unittest

from app.feishu.event_handler import FeishuEventHandler


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


if __name__ == "__main__":
    unittest.main()
