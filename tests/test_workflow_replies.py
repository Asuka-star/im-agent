import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.task_run import TaskRunDetail
from app.services.workflow.replies import WorkflowReplySender


class _FakeMessageAPI:
    def __init__(self) -> None:
        self.text_calls: list[dict] = []
        self.card_calls: list[dict] = []

    def send_text_message(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id") -> dict:
        self.text_calls.append({"receive_id": receive_id, "text": text, "receive_id_type": receive_id_type})
        return {"code": 0}

    def send_interactive_message(self, receive_id: str, card: dict, *, receive_id_type: str = "chat_id") -> dict:
        self.card_calls.append({"receive_id": receive_id, "card": card, "receive_id_type": receive_id_type})
        return {"code": 0}


class WorkflowReplySenderTests(unittest.TestCase):
    def test_artifact_records_keep_preview_for_reply_checks(self) -> None:
        records = WorkflowReplySender.artifact_records_from_payloads(
            [
                {
                    "artifact_type": "canvas",
                    "title": "风险图",
                    "url": "/api/artifacts/canvas/run.html",
                    "preview": {
                        "schema": "im-agent.canvas.v1",
                        "shapes": [{"id": "n1", "type": "node"}],
                        "exports": {"json": "/run.json", "svg": "/run.svg", "html": "/run.html"},
                    },
                }
            ]
        )

        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0].preview_json)
        self.assertIn("im-agent.canvas.v1", records[0].preview_json or "")

    def test_append_artifact_checks_to_reply_adds_im_summary(self) -> None:
        sender = WorkflowReplySender(SimpleNamespace())
        detail = TaskRunDetail(
            task_run_id="run_reply",
            session_id="session_1",
            source_type="group",
            title="生成场景 C/D 产物",
            stage="delivered",
            status="completed",
            created_at=datetime.now(timezone.utc),
            artifacts=WorkflowReplySender.artifact_records_from_payloads(
                [
                    {
                        "artifact_type": "canvas",
                        "title": "风险图",
                        "url": "/api/artifacts/canvas/run.html",
                        "preview": {
                            "schema": "im-agent.canvas.v1",
                            "shapes": [{"id": "n1", "type": "node"}],
                            "exports": {"json": "/run.json", "svg": "/run.svg", "html": "/run.html"},
                        },
                    },
                    {
                        "artifact_type": "slides_package",
                        "title": "汇报 PPT",
                        "url": "/api/artifacts/slides/run.html",
                        "preview": {
                            "slides": [
                                {
                                    "title": "开场",
                                    "speaker_notes": "介绍项目价值。",
                                    "duration_sec": 30,
                                }
                            ],
                            "exports": {"html": "/run.html", "pptx": "/run.pptx"},
                        },
                    },
                ]
            ),
        )

        reply = sender.append_artifact_checks_to_reply("已生成文档和汇报材料。", detail)

        self.assertIn("验收摘要：", reply)
        self.assertIn("白板 / Canvas：已满足", reply)
        self.assertIn("演示稿：已满足", reply)
        self.assertIn("排练辅助：已满足", reply)

    def test_deliver_reply_sends_best_effort_artifact_card_when_enabled(self) -> None:
        message_api = _FakeMessageAPI()
        workflow = SimpleNamespace(message_api=message_api)
        sender = WorkflowReplySender(workflow)
        message = SimpleNamespace(session_id="session_1", chat_id="chat_1")
        artifacts = [
            {
                "artifact_type": "canvas",
                "title": "流程图",
                "url": "/api/artifacts/canvas/run.html",
                "preview": {"summary": {"node_count": 3, "arrow_count": 2}},
            }
        ]

        with patch("app.services.workflow.replies.settings.feishu_reply_enabled", True), patch(
            "app.services.workflow.replies.settings.feishu_reply_card_enabled",
            True,
        ), patch("app.services.feishu_card_builder.settings.artifact_public_base_url", "https://demo.example"):
            result = sender.deliver_reply(
                message,
                "canvas",
                "已生成流程图。",
                analysis=None,
                artifacts=artifacts,
                append_next_actions=False,
            )

        self.assertTrue(result["reply_sent"])
        self.assertTrue(result["reply_card_sent"])
        self.assertEqual(message_api.text_calls[0]["receive_id"], "chat_1")
        self.assertEqual(message_api.card_calls[0]["receive_id"], "chat_1")
        action = next(item for item in message_api.card_calls[0]["card"]["elements"] if item.get("tag") == "action")
        self.assertEqual(action["actions"][0]["url"], "https://demo.example/api/artifacts/canvas/run.html")

    def test_assignment_clarification_uses_resume_option_card(self) -> None:
        message_api = _FakeMessageAPI()
        workflow = SimpleNamespace(message_api=message_api)
        sender = WorkflowReplySender(workflow)
        message = SimpleNamespace(session_id="session_1", chat_id="chat_1", message_id="om_1")

        sent = sender.send_clarification_card(
            message,
            intent="tasks",
            clarification={
                "question": "Which task should Bob take?",
                "reason": "Multiple candidates",
                "options": ["Bob - Backend development", "Bob - API integration"],
                "candidates": [
                    {"title": "Backend development", "owner": "Alice", "status": "draft"},
                    {"title": "API integration", "owner": "Alice", "status": "draft"},
                ],
                "target_status": "draft",
            },
            task_run_id="run_1",
            confirmation_id="confirm_1",
        )

        self.assertTrue(sent)
        action = next(item for item in message_api.card_calls[0]["card"]["elements"] if item.get("tag") == "action")
        self.assertEqual(action["actions"][0]["value"]["action"], "select_clarification_option")
        self.assertEqual(action["actions"][0]["value"]["payload"]["confirmation_id"], "confirm_1")

    def test_task_candidates_without_target_status_do_not_default_to_done_card(self) -> None:
        message_api = _FakeMessageAPI()
        workflow = SimpleNamespace(message_api=message_api)
        sender = WorkflowReplySender(workflow)
        message = SimpleNamespace(session_id="session_1", chat_id="chat_1", message_id="om_1")

        sent = sender.send_clarification_card(
            message,
            intent="tasks",
            clarification={
                "question": "Which task?",
                "reason": "Candidate list is ambiguous",
                "options": ["Alice - Backend development"],
                "candidates": [
                    {"title": "Backend development", "owner": "Alice", "status": "draft"},
                ],
            },
            task_run_id="run_1",
            confirmation_id="confirm_1",
        )

        self.assertTrue(sent)
        action = next(item for item in message_api.card_calls[0]["card"]["elements"] if item.get("tag") == "action")
        self.assertEqual(action["actions"][0]["value"]["action"], "select_clarification_option")

    def test_requirement_clarification_card_lists_recent_requirements_and_keeps_other_action(self) -> None:
        message_api = _FakeMessageAPI()
        workflow = SimpleNamespace(message_api=message_api)
        sender = WorkflowReplySender(workflow)
        message = SimpleNamespace(session_id="session_1", chat_id="chat_1", message_id="om_1")

        sent = sender.send_clarification_card(
            message,
            intent="requirement",
            clarification={
                "question": "我需要确认这次操作属于以下哪个需求，还是其他新需求。",
                "reason": "LLM 判断存在多个可能归属。",
                "options": [
                    "1. 校园活动报名系统",
                    "2. 社团审核后台",
                    "3. 学院老师数据看板",
                    "4. 活动流程图优化",
                    "5. 跨会话演示稿需求",
                    "6. 其他 / 新建一个需求",
                ],
            },
            task_run_id="run_1",
            confirmation_id="confirm_1",
        )

        self.assertTrue(sent)
        card = message_api.card_calls[0]["card"]
        body = "\n".join(
            item.get("text", {}).get("content", "")
            for item in card["elements"]
            if item.get("tag") == "div"
        )
        self.assertIn("3. 学院老师数据看板", body)
        self.assertIn("6. 其他 / 新建一个需求", body)
        action = next(item for item in card["elements"] if item.get("tag") == "action")
        self.assertEqual(len(action["actions"]), 3)
        self.assertEqual(action["actions"][-1]["value"]["payload"]["option"], "6. 其他 / 新建一个需求")


if __name__ == "__main__":
    unittest.main()
