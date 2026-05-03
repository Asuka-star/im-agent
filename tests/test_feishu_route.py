import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.api.routes import feishu


class _FakeRequest:
    async def json(self) -> dict:
        return {"event": "payload"}


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))


class FeishuRouteTests(unittest.TestCase):
    def _envelope(self, message_id: str = "om_1"):
        return SimpleNamespace(
            type=None,
            challenge=None,
            header=SimpleNamespace(event_type="im.message.receive_v1"),
            event=SimpleNamespace(message=SimpleNamespace(message_id=message_id)),
        )

    def test_receive_events_acknowledges_before_workflow_processing(self) -> None:
        background = _FakeBackgroundTasks()
        fake_handler = Mock()
        fake_handler.parse_event.return_value = self._envelope("om_fast")
        fake_handler.is_url_verification.return_value = False
        fake_handler.verify_token.return_value = True
        fake_dedup = Mock()
        fake_dedup.accept_for_processing.return_value = True

        with patch.object(feishu, "event_handler", fake_handler), patch.object(
            feishu,
            "dedup_service",
            fake_dedup,
        ), patch.object(feishu.workflow_service, "handle_message") as handle_message:
            response = asyncio.run(feishu.receive_events(_FakeRequest(), background))

        self.assertEqual(response["msg"], "accepted")
        self.assertTrue(response["data"]["background"])
        self.assertEqual(len(background.tasks), 1)
        handle_message.assert_not_called()
        fake_dedup.accept_for_processing.assert_called_once_with("om_fast")

    def test_receive_events_does_not_schedule_duplicate_messages(self) -> None:
        background = _FakeBackgroundTasks()
        fake_handler = Mock()
        fake_handler.parse_event.return_value = self._envelope("om_duplicate")
        fake_handler.is_url_verification.return_value = False
        fake_handler.verify_token.return_value = True
        fake_dedup = Mock()
        fake_dedup.accept_for_processing.return_value = False

        with patch.object(feishu, "event_handler", fake_handler), patch.object(
            feishu,
            "dedup_service",
            fake_dedup,
        ):
            response = asyncio.run(feishu.receive_events(_FakeRequest(), background))

        self.assertEqual(response["msg"], "duplicate_ignored")
        self.assertEqual(background.tasks, [])

    def test_receive_events_schedules_card_action_without_message_dedup(self) -> None:
        background = _FakeBackgroundTasks()
        fake_handler = Mock()
        fake_handler.parse_event.return_value = SimpleNamespace(
            type=None,
            challenge=None,
            header=SimpleNamespace(event_type="card.action.trigger", event_id="evt_card"),
            event=None,
        )
        fake_handler.is_url_verification.return_value = False
        fake_handler.verify_token.return_value = True
        fake_dedup = Mock()

        class _CardRequest:
            async def json(self) -> dict:
                return {
                    "header": {"event_type": "card.action.trigger", "event_id": "evt_card"},
                    "event": {
                        "action": {
                            "value": {
                                "action": "cancel_task_update",
                                "idempotency_key": "card_1",
                                "payload": {},
                            }
                        }
                    },
                }

        with patch.object(feishu, "event_handler", fake_handler), patch.object(
            feishu,
            "dedup_service",
            fake_dedup,
        ):
            response = asyncio.run(feishu.receive_events(_CardRequest(), background))

        self.assertEqual(response["msg"], "accepted")
        self.assertEqual(response["data"]["kind"], "card_action")
        self.assertEqual(len(background.tasks), 1)
        fake_dedup.accept_for_processing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
