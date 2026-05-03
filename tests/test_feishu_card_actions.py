import unittest
from types import SimpleNamespace

from app.schemas.task import TaskItem
from app.services.cards.action_handler import FeishuCardActionService
from app.services.cards.action_payloads import build_card_action_payload
from app.services.cards.builders import FeishuCardBuilder
from app.services.response_formatter import ResponseFormatter


class _FakeMemoryService:
    def __init__(self, tasks: list[TaskItem]) -> None:
        self.tasks = tasks
        self.saved_analysis = None

    def get_current_tasks(self, session_id: str):
        return [
            SimpleNamespace(
                title=task.title,
                owner=task.owner,
                priority=task.priority,
                due_date=task.due_date,
                status=task.status,
                notes=task.notes,
            )
            for task in self.tasks
        ]

    def save_round(self, **kwargs):
        self.saved_analysis = kwargs["analysis"]
        self.tasks = list(self.saved_analysis.tasks)


class _FakeTaskRunService:
    def __init__(self) -> None:
        self.steps = []
        self.updates = []
        self.resolved = []
        self.detail = None

    def upsert_step(self, *args, **kwargs):
        self.steps.append((args, kwargs))

    def update_task_run(self, *args, **kwargs):
        self.updates.append((args, kwargs))

    def resolve_confirmation(self, *args, **kwargs):
        self.resolved.append((args, kwargs))

    def get_task_run(self, task_run_id: str):
        return self.detail


class _FakeMessageAPI:
    def __init__(self) -> None:
        self.text_messages = []
        self.patched_cards = []

    def send_text_message(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id"):
        self.text_messages.append(
            {"receive_id": receive_id, "text": text, "receive_id_type": receive_id_type}
        )
        return {"code": 0}

    def patch_card(self, message_id: str, card: dict):
        self.patched_cards.append({"message_id": message_id, "card": card})
        return {"code": 0}


class _FakeWorkflow:
    def __init__(self, tasks: list[TaskItem]) -> None:
        self.memory_service = _FakeMemoryService(tasks)
        self.task_run_service = _FakeTaskRunService()
        self.message_api = _FakeMessageAPI()
        self.response_formatter = ResponseFormatter()

    def _base_status_task_sources_for_message(self, _message):
        return [], list(self.memory_service.tasks), list(self.memory_service.tasks)

    def bundle_delivery_from_task_run(self, task_run_id: str, *, requested_by: str):
        self.bundle_request = {"task_run_id": task_run_id, "requested_by": requested_by}
        return SimpleNamespace(task_run_id=task_run_id)

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
    ):
        self.resume_request = {
            "task_run_id": task_run_id,
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
        }
        return {"reply_preview": f"resumed: {answer_value}"}


class FeishuCardActionTests(unittest.TestCase):
    def test_task_confirmation_card_uses_structured_action_values(self) -> None:
        card = FeishuCardBuilder().build_task_confirmation_card(
            session_id="oc_1",
            task_run_id="run_1",
            source_message_id="om_1",
            question="Which task?",
            reason="Multiple candidates",
            candidates=[
                TaskItem(title="Backend development", owner="Alice", status="draft"),
                TaskItem(title="API integration", owner="Alice", status="draft"),
            ],
            target_status="done",
            confirmation_id="confirm_1",
        )

        self.assertIsNotNone(card)
        action = next(item for item in card["elements"] if item.get("tag") == "action")
        first_value = action["actions"][0]["value"]
        self.assertEqual(first_value["action"], "confirm_task_status")
        self.assertEqual(first_value["payload"]["task"]["title"], "Backend development")
        self.assertEqual(first_value["payload"]["target_status"], "done")
        self.assertEqual(first_value["payload"]["confirmation_id"], "confirm_1")

    def test_confirm_task_status_updates_only_selected_task(self) -> None:
        workflow = _FakeWorkflow(
            [
                TaskItem(title="Backend development", owner="Alice", status="draft"),
                TaskItem(title="API integration", owner="Alice", status="draft"),
            ]
        )
        service = FeishuCardActionService(workflow)
        value = build_card_action_payload(
            "confirm_task_status",
            session_id="oc_1",
            task_run_id="run_1",
            source_message_id="om_1",
            payload={
                "task": {"title": "API integration", "owner": "Alice", "status": "draft"},
                "target_status": "done",
                "confirmation_id": "confirm_1",
            },
        )

        result = service.handle_raw_event(
            {
                "header": {"event_type": "card.action.trigger", "event_id": "evt_1"},
                "event": {
                    "message": {"message_id": "card_msg", "chat_id": "oc_1"},
                    "operator": {"user_id": "ou_1"},
                    "action": {"value": value},
                },
            }
        )

        self.assertEqual(result["msg"], "handled")
        statuses = {task.title: task.status for task in workflow.memory_service.tasks}
        self.assertEqual(statuses["Backend development"], "draft")
        self.assertEqual(statuses["API integration"], "done")
        self.assertEqual(workflow.task_run_service.resolved[0][1]["confirmation_id"], "confirm_1")
        self.assertTrue(workflow.message_api.text_messages)
        self.assertEqual(workflow.message_api.patched_cards[0]["message_id"], "card_msg")

    def test_duplicate_card_action_is_ignored(self) -> None:
        workflow = _FakeWorkflow([TaskItem(title="Backend development", owner="Alice", status="draft")])
        service = FeishuCardActionService(workflow)
        value = build_card_action_payload(
            "confirm_task_status",
            session_id="oc_1",
            task_run_id="run_1",
            payload={
                "task": {"title": "Backend development", "owner": "Alice"},
                "target_status": "done",
            },
            idempotency_key="fixed-key",
        )
        payload = {
            "header": {"event_type": "card.action.trigger"},
            "event": {"message": {"chat_id": "oc_1"}, "action": {"value": value}},
        }

        first = service.handle_raw_event(payload)
        second = service.handle_raw_event(payload)

        self.assertEqual(first["msg"], "handled")
        self.assertEqual(second["msg"], "duplicate_ignored")
        self.assertEqual(len(workflow.task_run_service.steps), 1)

    def test_select_clarification_option_resumes_task_run_and_patches_card(self) -> None:
        workflow = _FakeWorkflow([TaskItem(title="Backend development", owner="Alice", status="draft")])
        service = FeishuCardActionService(workflow)
        value = build_card_action_payload(
            "select_clarification_option",
            session_id="oc_1",
            task_run_id="run_1",
            source_message_id="om_1",
            payload={
                "option": "改为新负责人：Bob - Backend development",
                "confirmation_id": "confirm_1",
            },
        )

        result = service.handle_raw_event(
            {
                "header": {"event_type": "card.action.trigger", "event_id": "evt_2"},
                "event": {
                    "message": {"message_id": "card_msg_2", "chat_id": "oc_1"},
                    "operator": {"user_id": "ou_1"},
                    "action": {"value": value},
                },
            }
        )

        self.assertEqual(result["msg"], "handled")
        self.assertTrue(result["data"]["resumed"])
        self.assertEqual(workflow.resume_request["confirmation_id"], "confirm_1")
        self.assertIn("Bob", workflow.resume_request["answer_value"])
        self.assertEqual(workflow.message_api.patched_cards[0]["message_id"], "card_msg_2")

    def test_answered_confirmation_is_not_reprocessed_after_restart(self) -> None:
        workflow = _FakeWorkflow([TaskItem(title="Backend development", owner="Alice", status="draft")])
        workflow.task_run_service.detail = SimpleNamespace(
            confirmations=[
                SimpleNamespace(confirmation_id="confirm_1", status="answered"),
            ]
        )
        service = FeishuCardActionService(workflow)
        value = build_card_action_payload(
            "confirm_task_status",
            session_id="oc_1",
            task_run_id="run_1",
            payload={
                "task": {"title": "Backend development", "owner": "Alice"},
                "target_status": "done",
                "confirmation_id": "confirm_1",
            },
            idempotency_key="fresh-process-key",
        )

        result = service.handle_raw_event(
            {
                "header": {"event_type": "card.action.trigger"},
                "event": {
                    "message": {"message_id": "card_msg_3", "chat_id": "oc_1"},
                    "action": {"value": value},
                },
            }
        )

        self.assertEqual(result["msg"], "duplicate_ignored")
        self.assertEqual(workflow.memory_service.tasks[0].status, "draft")
        self.assertFalse(workflow.task_run_service.steps)
        self.assertEqual(workflow.message_api.patched_cards[0]["message_id"], "card_msg_3")


if __name__ == "__main__":
    unittest.main()
