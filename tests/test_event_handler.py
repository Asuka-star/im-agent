import unittest
from types import SimpleNamespace

from app.feishu.event_handler import FeishuEventHandler
from app.schemas.feishu_event import FeishuMessageLifecycleContext
from app.services.feishu_workflow import FeishuWorkflowService


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
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-1", "tenant_key": "tenant-demo"},
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
        self.assertEqual(context.tenant_key, "tenant-demo")
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

    def test_plain_text_bot_prefix_marks_request_as_trigger_without_mentions(self) -> None:
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-text-prefix"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-text-prefix",
                    "chat_id": "chat-1",
                    "chat_type": "group",
                    "content": "{\"text\":\"@机器人 下一步行动是什么？\"}",
                    "mentions": [],
                },
            },
        }

        context = self.handler.extract_message_context(self.handler.parse_event(payload))
        assert context is not None
        self.assertTrue(context.is_mentioned)
        self.assertEqual(context.text, "下一步行动是什么？")

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

    def test_file_message_uses_file_name_when_no_caption_is_present(self) -> None:
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-file"},
            "event": {
                "sender": {"sender_id": {"user_id": "speaker-1"}},
                "message": {
                    "message_id": "msg-file",
                    "chat_id": "chat-1",
                    "chat_type": "p2p",
                    "message_type": "file",
                    "content": "{\"file_key\":\"file-doc-1\",\"file_name\":\"评审纪要.docx\"}",
                },
            },
        }

        context = self.handler.extract_message_context(self.handler.parse_event(payload))
        assert context is not None
        self.assertEqual(context.message_type, "file")
        self.assertEqual(context.file_key, "file-doc-1")
        self.assertEqual(context.file_name, "评审纪要.docx")
        self.assertEqual(context.text, "评审纪要.docx")
        self.assertEqual(context.raw_text, "评审纪要.docx")

    def test_recalled_message_event_extracts_lifecycle_context(self) -> None:
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": "evt-recall",
                "event_type": "im.message.recalled_v1",
                "tenant_key": "tenant-demo",
            },
            "event": {
                "message_id": "om_recalled",
                "chat_id": "oc_demo",
                "recall_time": "1615380573411",
                "recall_type": "message_owner",
            },
        }

        envelope = self.handler.parse_event(payload)
        context = self.handler.extract_message_lifecycle_context(payload)

        self.assertTrue(self.handler.is_message_lifecycle_event(envelope))
        assert context is not None
        self.assertEqual(context.event_type, "im.message.recalled_v1")
        self.assertEqual(context.message_id, "om_recalled")
        self.assertEqual(context.session_id, "oc_demo")
        self.assertEqual(context.recall_type, "message_owner")

    def test_updated_message_event_extracts_new_text(self) -> None:
        payload = {
            "header": {"event_id": "evt-update", "event_type": "im.message.updated_v1"},
            "event": {
                "message": {
                    "message_id": "om_updated",
                    "chat_id": "oc_demo",
                    "message_type": "text",
                    "content": "{\"text\":\"修正后的任务描述\"}",
                }
            },
        }

        context = self.handler.extract_message_lifecycle_context(payload)

        assert context is not None
        self.assertEqual(context.event_type, "im.message.updated_v1")
        self.assertEqual(context.message_id, "om_updated")
        self.assertEqual(context.raw_text, "修正后的任务描述")

    def test_lifecycle_impact_marks_related_runs_and_documents_dirty(self) -> None:
        class FakeMemoryService:
            def get_message_lifecycle_info(self, message_id: str) -> dict:
                return {"message_id": message_id, "session_id": "oc_demo", "episode_id": 7}

            def mark_episode_outputs_source_dirty(self, **kwargs) -> dict:
                self.dirty_call = kwargs
                return {"memory_count": 1, "chunk_count": 2}

        class FakeTaskRunService:
            def __init__(self) -> None:
                self.metadata_patches: list[tuple[str, dict]] = []
                self.steps: list[tuple[str, dict]] = []

            def list_task_runs(self, *, session_id: str | None = None, limit: int = 20):
                return [
                    SimpleNamespace(task_run_id="run_direct", trigger_message_id="om_changed"),
                    SimpleNamespace(task_run_id="run_other", trigger_message_id="om_other"),
                ]

            def merge_task_run_metadata(self, task_run_id: str, patch: dict):
                self.metadata_patches.append((task_run_id, patch))

            def upsert_step(self, task_run_id: str, **kwargs):
                self.steps.append((task_run_id, kwargs))

        class FakeSessionDocumentService:
            def mark_documents_source_dirty(self, session_id: str, **kwargs):
                self.call = {"session_id": session_id, **kwargs}
                return [{"document_id": "doc_dirty", "task_run_id": "run_doc"}]

        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        workflow.memory_service = FakeMemoryService()
        workflow.task_run_service = FakeTaskRunService()
        workflow.session_document_service = FakeSessionDocumentService()
        event = FeishuMessageLifecycleContext(
            event_type="im.message.recalled_v1",
            message_id="om_changed",
            chat_id="oc_demo",
            session_id="oc_demo",
        )

        impact = workflow._mark_message_lifecycle_impacts(event, action="recalled")

        self.assertEqual(impact["task_run_ids"], ["run_direct", "run_doc"])
        self.assertEqual(impact["document_ids"], ["doc_dirty"])
        self.assertEqual(impact["memory_count"], 1)
        self.assertEqual(impact["chunk_count"], 2)
        self.assertEqual(workflow.memory_service.dirty_call["episode_id"], 7)
        self.assertEqual(workflow.session_document_service.call["episode_id"], 7)
        self.assertEqual(workflow.session_document_service.call["task_run_ids"], ["run_direct"])
        self.assertEqual(
            [task_run_id for task_run_id, _ in workflow.task_run_service.metadata_patches],
            ["run_direct", "run_doc"],
        )
        self.assertTrue(workflow.task_run_service.metadata_patches[0][1]["source_dirty"])
        self.assertEqual(workflow.task_run_service.steps[0][1]["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
