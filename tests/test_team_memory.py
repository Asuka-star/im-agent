import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AppSetting, Episode, Memory, MemoryChunk, Message, Session, Task, TaskChangeLog, UserAlias
from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService
from app.services.session_document_service import SessionDocumentService
from app.services.workflow.entrypoint import WorkflowEntrypoint


class TeamMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tempdir.name, "team_memory.db")
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        for table in (
            AppSetting.__table__,
            Session.__table__,
            Episode.__table__,
            Message.__table__,
            Task.__table__,
            TaskChangeLog.__table__,
            Memory.__table__,
            MemoryChunk.__table__,
            UserAlias.__table__,
        ):
            table.create(bind=self.engine)

        memory_patcher = patch("app.services.memory_service.SessionLocal", self.test_session_local)
        state_patcher = patch("app.services.app_state.SessionLocal", self.test_session_local)
        self.addCleanup(memory_patcher.stop)
        self.addCleanup(state_patcher.stop)
        memory_patcher.start()
        state_patcher.start()
        self.service = MemoryService()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_p2p_team_context_can_read_registered_group_discussion(self) -> None:
        self.service.register_team_group_session("tenant_a", "oc_group_a")
        episode = self.service.ensure_active_episode("oc_group_a")
        self.service.save_user_message(
            session_id="oc_group_a",
            message_id="msg_group_1",
            sender_id="u1",
            content="张三负责后端接口，周五前给出联调版本。",
            episode_id=episode.id,
            embed=False,
        )
        self.service.save_round(
            session_id="oc_group_a",
            analysis=AnalyzeResponse(
                session_id="oc_group_a",
                summary="群聊明确了后端接口联调安排。",
                tasks=[
                    TaskItem(
                        title="后端接口联调",
                        owner="张三",
                        priority="medium",
                        due_date="周五",
                        status="draft",
                        notes="群聊中明确",
                    )
                ],
                risks=[],
                next_actions=["张三周五前提交联调版本"],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            embed=False,
        )

        context = self.service.build_team_workspace_context(
            "tenant_a",
            current_session_id="ou_p2p_a",
            query_text="当前后端是谁负责",
        )
        tasks = self.service.get_team_current_tasks("tenant_a", current_session_id="ou_p2p_a")

        self.assertIn("[团队群聊上下文]", context)
        self.assertIn("张三负责后端接口", context)
        self.assertIn("后端接口联调", context)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "张三")

    def test_lifecycle_context_keeps_discussion_before_task_snapshot(self) -> None:
        episode = self.service.ensure_active_episode("oc_lifecycle_context")
        self.service.save_user_message(
            session_id="oc_lifecycle_context",
            message_id="msg_req_1",
            sender_id="u1",
            content="我们要做校园活动报名系统，目标用户是学生、社团负责人和学院老师。",
            episode_id=episode.id,
            embed=False,
        )
        self.service.save_round(
            session_id="oc_lifecycle_context",
            analysis=AnalyzeResponse(
                session_id="oc_lifecycle_context",
                summary="已整理实施任务。",
                tasks=[
                    TaskItem(
                        title="前端页面开发",
                        owner="张三",
                        priority="medium",
                        due_date="TBD",
                        status="draft",
                        notes="实施计划",
                    )
                ],
                risks=[],
                next_actions=[],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            embed=False,
        )

        lifecycle_context = self.service.build_workspace_context(
            "oc_lifecycle_context",
            profile="lifecycle",
            episode_id=episode.id,
        )
        task_context = self.service.build_workspace_context(
            "oc_lifecycle_context",
            profile="task",
            episode_id=episode.id,
        )

        self.assertIn("[近期群聊讨论]", lifecycle_context)
        self.assertIn("[实施计划参考]", lifecycle_context)
        self.assertLess(lifecycle_context.index("[近期群聊讨论]"), lifecycle_context.index("[实施计划参考]"))
        self.assertLess(task_context.index("[当前任务快照]"), task_context.index("[近期群聊讨论]"))

    def test_recalled_message_is_removed_from_context_and_chunks(self) -> None:
        episode = self.service.ensure_active_episode("oc_group_recall")
        self.service.save_user_message(
            session_id="oc_group_recall",
            message_id="msg_recalled",
            sender_id="u1",
            content="这条任务安排后来被撤回。",
            episode_id=episode.id,
            embed=False,
        )

        updated = self.service.mark_message_recalled(
            message_id="msg_recalled",
            chat_id="oc_group_recall",
            recall_time="1615380573411",
            recall_type="message_owner",
        )
        context = self.service.build_discussion_block("oc_group_recall", episode_id=episode.id)
        with self.test_session_local() as session:
            chunk_count = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_recalled").count()
            message = session.query(Message).filter(Message.message_id == "msg_recalled").one()

        self.assertTrue(updated)
        self.assertEqual(message.status, "recalled")
        self.assertNotIn("这条任务安排后来被撤回", context)
        self.assertEqual(chunk_count, 0)

    def test_updated_message_replaces_context_and_memory_chunk(self) -> None:
        episode = self.service.ensure_active_episode("oc_group_update")
        self.service.save_user_message(
            session_id="oc_group_update",
            message_id="msg_updated",
            sender_id="u1",
            content="旧任务描述",
            episode_id=episode.id,
            embed=False,
        )

        updated = self.service.update_user_message_content(
            message_id="msg_updated",
            content="新任务描述",
            chat_id="oc_group_update",
        )
        context = self.service.build_discussion_block("oc_group_update", episode_id=episode.id)
        with self.test_session_local() as session:
            chunks = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_updated").all()
            message = session.query(Message).filter(Message.message_id == "msg_updated").one()

        self.assertTrue(updated)
        self.assertEqual(message.original_content, "旧任务描述")
        self.assertEqual(message.content, "新任务描述")
        self.assertIn("新任务描述", context)
        self.assertNotIn("旧任务描述", context)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "新任务描述")

    def test_recall_before_receive_keeps_late_original_message_out_of_context(self) -> None:
        updated = self.service.mark_message_recalled(
            message_id="msg_recall_before_receive",
            chat_id="oc_group_ordering",
            recall_time="1615380573411",
            recall_type="message_owner",
        )
        episode = self.service.ensure_active_episode("oc_group_ordering")
        self.service.save_user_message(
            session_id="oc_group_ordering",
            message_id="msg_recall_before_receive",
            sender_id="u1",
            content="这条原始消息晚到了，但已经撤回。",
            episode_id=episode.id,
            embed=False,
        )
        context = self.service.build_discussion_block("oc_group_ordering", episode_id=episode.id)
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_recall_before_receive").one()

        self.assertFalse(updated)
        self.assertEqual(message.status, "recalled")
        self.assertNotIn("这条原始消息晚到了", context)

    def test_recall_before_receive_without_chat_id_adopts_late_session(self) -> None:
        self.service.mark_message_recalled(
            message_id="msg_recall_without_chat",
            chat_id=None,
            recall_time="1615380573411",
            recall_type="message_owner",
        )
        self.service.save_user_message(
            session_id="oc_real_recall_session",
            message_id="msg_recall_without_chat",
            sender_id="u1",
            content="late recalled original",
            episode_id=None,
            embed=False,
        )
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_recall_without_chat").one()

        self.assertEqual(message.status, "recalled")
        self.assertEqual(message.session_id, "oc_real_recall_session")
        self.assertEqual(message.sender_id, "u1")

    def test_update_before_receive_keeps_edited_text_when_original_arrives_late(self) -> None:
        updated = self.service.update_user_message_content(
            message_id="msg_update_before_receive",
            content="编辑后的消息",
            chat_id="oc_group_update_ordering",
        )
        episode = self.service.ensure_active_episode("oc_group_update_ordering")
        self.service.save_user_message(
            session_id="oc_group_update_ordering",
            message_id="msg_update_before_receive",
            sender_id="u1",
            content="原始消息晚到了",
            episode_id=episode.id,
            embed=False,
        )
        context = self.service.build_discussion_block("oc_group_update_ordering", episode_id=episode.id)
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_update_before_receive").one()
            chunk = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_update_before_receive").one()
        chunk_metadata = json.loads(chunk.metadata_json)

        self.assertTrue(updated)
        self.assertEqual(message.status, "active")
        self.assertEqual(message.content, "编辑后的消息")
        self.assertEqual(message.sender_id, "u1")
        self.assertEqual(message.episode_id, episode.id)
        self.assertEqual(chunk_metadata["sender_id"], "u1")
        self.assertEqual(chunk_metadata["episode_id"], episode.id)
        self.assertIn("编辑后的消息", context)
        self.assertNotIn("原始消息晚到了", context)

    def test_update_before_receive_without_chat_id_moves_placeholder_chunk_to_late_session(self) -> None:
        updated = self.service.update_user_message_content(
            message_id="msg_update_without_chat",
            content="edited before chat known",
            chat_id=None,
        )
        self.service.save_user_message(
            session_id="oc_real_update_session",
            message_id="msg_update_without_chat",
            sender_id="u1",
            content="late original content",
            episode_id=None,
            embed=False,
        )
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_update_without_chat").one()
            chunks = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_update_without_chat").all()

        self.assertTrue(updated)
        self.assertEqual(message.session_id, "oc_real_update_session")
        self.assertEqual(message.content, "edited before chat known")
        self.assertEqual(message.original_content, "late original content")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].session_id, "oc_real_update_session")

    def test_update_before_receive_strips_late_known_mention_key_from_placeholder(self) -> None:
        self.service.update_user_message_content(
            message_id="msg_update_mention_key",
            content="@_user_1 edited command",
            chat_id=None,
        )
        self.service.save_user_message(
            session_id="oc_real_update_mention",
            message_id="msg_update_mention_key",
            sender_id="u1",
            content="original command",
            episode_id=None,
            mentioned_users=[{"key": "@_user_1", "name": "bot", "is_bot": True}],
            embed=False,
        )
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_update_mention_key").one()
            chunks = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_update_mention_key").all()

        self.assertEqual(message.content, "edited command")
        self.assertEqual(message.original_content, "original command")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "edited command")

    def test_second_update_with_chat_id_moves_placeholder_before_receive(self) -> None:
        self.service.update_user_message_content(
            message_id="msg_update_chat_later",
            content="first edit without chat",
            chat_id=None,
        )

        updated = self.service.update_user_message_content(
            message_id="msg_update_chat_later",
            content="second edit with chat",
            chat_id="oc_update_chat_later",
        )
        info = self.service.get_message_lifecycle_info("msg_update_chat_later")
        with self.test_session_local() as session:
            chunks = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_update_chat_later").all()

        self.assertTrue(updated)
        self.assertEqual(info["session_id"], "oc_update_chat_later")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].session_id, "oc_update_chat_later")

    def test_recall_with_chat_id_moves_update_placeholder_before_receive(self) -> None:
        self.service.update_user_message_content(
            message_id="msg_update_then_recall_chat_later",
            content="edited before chat known",
            chat_id=None,
        )

        updated = self.service.mark_message_recalled(
            message_id="msg_update_then_recall_chat_later",
            chat_id="oc_recall_chat_later",
            recall_time="1615380573411",
            recall_type="message_owner",
        )
        info = self.service.get_message_lifecycle_info("msg_update_then_recall_chat_later")
        with self.test_session_local() as session:
            chunks = session.query(MemoryChunk).filter(MemoryChunk.source_id == "msg_update_then_recall_chat_later").all()

        self.assertTrue(updated)
        self.assertEqual(info["session_id"], "oc_recall_chat_later")
        self.assertEqual(info["status"], "recalled")
        self.assertEqual(chunks, [])

    def test_update_after_recall_does_not_resurrect_message(self) -> None:
        self.service.mark_message_recalled(
            message_id="msg_recall_then_update",
            chat_id="oc_group_recall_then_update",
            recall_time="1615380573411",
            recall_type="message_owner",
        )

        updated = self.service.update_user_message_content(
            message_id="msg_recall_then_update",
            content="late edited content",
            chat_id="oc_group_recall_then_update",
        )
        content = self.service.get_user_message_content("msg_recall_then_update")
        with self.test_session_local() as session:
            message = session.query(Message).filter(Message.message_id == "msg_recall_then_update").one()

        self.assertFalse(updated)
        self.assertIsNone(content)
        self.assertEqual(message.status, "recalled")
        self.assertEqual(message.content, "[消息已撤回]")

    def test_late_receive_after_recall_is_skipped_before_task_run(self) -> None:
        self.service.mark_message_recalled(
            message_id="msg_late_receive_recalled",
            chat_id="ou_recalled_late",
            recall_time="1615380573411",
            recall_type="message_owner",
        )
        workflow = Mock()
        workflow.memory_service = self.service
        workflow._ensure_sender_alias.return_value = None
        workflow.task_run_service.create_task_run.side_effect = AssertionError("task run should not be created")
        entrypoint = WorkflowEntrypoint(workflow)

        result = entrypoint.handle_message(
            FeishuMessageContext(
                message_id="msg_late_receive_recalled",
                chat_id="ou_recalled_late",
                chat_type="p2p",
                message_type="text",
                session_id="ou_recalled_late",
                sender_id="u1",
                text="original content arrived late",
                raw_text="original content arrived late",
            )
        )

        self.assertEqual(result["mode"], "recalled_message_skipped")
        self.assertFalse(result["reply_sent"])

    def test_session_document_can_be_marked_dirty_by_changed_source_message(self) -> None:
        doc_service = SessionDocumentService()
        doc_service.save_current_document(
            "oc_dirty_doc",
            document_id="doc_dirty",
            url="https://feishu.cn/docx/doc_dirty",
            title="协作文档",
            episode_id=42,
            task_run_id="run_doc_dirty",
            section_snapshot=[{"heading": "任务清单", "paragraphs": ["旧内容"]}],
        )

        current_doc = doc_service.get_current_document("oc_dirty_doc") or {}
        current_doc["episode_id"] = "42"
        doc_service.state_service.set_value(doc_service._key("oc_dirty_doc"), json.dumps(current_doc, ensure_ascii=False))
        doc_service.state_service.set_value(doc_service._list_key("oc_dirty_doc"), json.dumps([current_doc], ensure_ascii=False))

        dirty_documents = doc_service.mark_documents_source_dirty(
            "oc_dirty_doc",
            message_id="msg_changed",
            episode_id=42,
            task_run_ids=[],
            event_type="im.message.recalled_v1",
            reason="源消息已撤回，相关产物需要复核。",
        )
        current = doc_service.get_current_document("oc_dirty_doc")

        self.assertEqual([item["document_id"] for item in dirty_documents], ["doc_dirty"])
        assert current is not None
        self.assertTrue(current["source_dirty"])
        self.assertEqual(current["source_dirty_message_id"], "msg_changed")

    def test_dirty_episode_hides_old_summary_and_task_changes_from_context(self) -> None:
        episode = self.service.ensure_active_episode("oc_dirty_memory")
        self.service.save_round(
            session_id="oc_dirty_memory",
            analysis=AnalyzeResponse(
                session_id="oc_dirty_memory",
                summary="旧总结来自后来撤回的消息。",
                tasks=[
                    TaskItem(
                        title="旧任务",
                        owner="张三",
                        priority="medium",
                        due_date="TBD",
                        status="draft",
                        notes="来自旧总结",
                    )
                ],
                risks=[],
                next_actions=[],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            embed=False,
        )

        impact = self.service.mark_episode_outputs_source_dirty(
            session_id="oc_dirty_memory",
            episode_id=episode.id,
            message_id="msg_dirty_source",
            event_type="im.message.recalled_v1",
            reason="源消息已撤回，相关任务快照和产物需要复核。",
        )
        context = self.service.build_workspace_context("oc_dirty_memory", include_pending=False)
        memories = self.service.get_recent_memories("oc_dirty_memory")
        changes = self.service.get_recent_task_changes("oc_dirty_memory")
        payload = self.service.load_memory_payload("oc_dirty_memory")

        self.assertEqual(impact["memory_count"], 1)
        self.assertGreaterEqual(impact["chunk_count"], 1)
        self.assertEqual(memories, [])
        self.assertEqual(changes, [])
        self.assertEqual(payload, {})
        self.assertIn("[源消息变更提醒]", context)
        self.assertIn("msg_dirty_source", context)
        self.assertNotIn("[最近总结]\n- 旧总结来自后来撤回的消息。", context)
        self.assertIn("旧任务", context)

    def test_dirty_episode_does_not_hide_new_clean_round_with_different_source(self) -> None:
        episode = self.service.ensure_active_episode("oc_dirty_then_clean")
        self.service.save_round(
            session_id="oc_dirty_then_clean",
            analysis=AnalyzeResponse(
                session_id="oc_dirty_then_clean",
                summary="old source summary",
                tasks=[
                    TaskItem(
                        title="old source task",
                        owner="u1",
                        priority="medium",
                        due_date="TBD",
                        status="draft",
                        notes="old",
                    )
                ],
                risks=[],
                next_actions=[],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            source_message_id="msg_dirty_old",
            embed=False,
            preserve_unmatched_previous=False,
        )
        self.service.mark_episode_outputs_source_dirty(
            session_id="oc_dirty_then_clean",
            episode_id=episode.id,
            message_id="msg_dirty_old",
            event_type="im.message.recalled_v1",
            reason="old source changed",
        )

        self.service.save_round(
            session_id="oc_dirty_then_clean",
            analysis=AnalyzeResponse(
                session_id="oc_dirty_then_clean",
                summary="new clean summary",
                tasks=[
                    TaskItem(
                        title="new clean task",
                        owner="u2",
                        priority="medium",
                        due_date="TBD",
                        status="draft",
                        notes="clean",
                    )
                ],
                risks=[],
                next_actions=[],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=episode.id,
            source_message_id="msg_clean_new",
            embed=False,
            preserve_unmatched_previous=False,
        )
        memories = self.service.get_recent_memories("oc_dirty_then_clean", limit=5)
        changes = self.service.get_recent_task_changes("oc_dirty_then_clean", limit=5)

        self.assertEqual([memory.summary for memory in memories], ["new clean summary"])
        self.assertTrue(any(change.title == "new clean task" for change in changes))
        self.assertFalse(any(change.title == "old source task" and change.action == "created" for change in changes))

    def test_dirty_source_message_hides_direct_round_without_episode(self) -> None:
        self.service.save_round(
            session_id="oc_dirty_direct",
            analysis=AnalyzeResponse(
                session_id="oc_dirty_direct",
                summary="direct dirty summary",
                tasks=[
                    TaskItem(
                        title="direct dirty task",
                        owner="u1",
                        priority="medium",
                        due_date="TBD",
                        status="draft",
                        notes="from direct message",
                    )
                ],
                risks=[],
                next_actions=[],
                agent_traces=[AgentTrace(agent="planner", summary="ok")],
            ),
            episode_id=None,
            source_message_id="msg_direct_dirty",
            embed=False,
            preserve_unmatched_previous=False,
        )
        self.service.save_assistant_message(
            session_id="oc_dirty_direct",
            content="assistant reply from direct dirty message",
            source_message_id="msg_direct_dirty",
            embed=False,
        )

        impact = self.service.mark_episode_outputs_source_dirty(
            session_id="oc_dirty_direct",
            episode_id=None,
            message_id="msg_direct_dirty",
            event_type="im.message.updated_v1",
            reason="direct source message changed",
        )
        context = self.service.build_workspace_context("oc_dirty_direct", include_pending=False)
        memories = self.service.get_recent_memories("oc_dirty_direct")
        changes = self.service.get_recent_task_changes("oc_dirty_direct")
        payload = self.service.load_memory_payload("oc_dirty_direct")
        with self.test_session_local() as session:
            assistant_chunks = (
                session.query(MemoryChunk)
                .filter(
                    MemoryChunk.session_id == "oc_dirty_direct",
                    MemoryChunk.source_type == "assistant_reply",
                )
                .all()
            )
        self.service.save_memory_chunk(
            session_id="oc_dirty_direct",
            source_type="summary",
            source_id=None,
            content="late async dirty summary chunk",
            metadata={"source_message_id": "msg_direct_dirty"},
            embed=False,
        )
        dirty_message_ids = self.service._dirty_source_message_ids("oc_dirty_direct")
        with self.test_session_local() as session:
            late_chunk = (
                session.query(MemoryChunk)
                .filter(MemoryChunk.content == "late async dirty summary chunk")
                .one()
            )

        self.assertEqual(impact["memory_count"], 1)
        self.assertGreaterEqual(impact["chunk_count"], 2)
        self.assertEqual(memories, [])
        self.assertEqual(changes, [])
        self.assertEqual(payload, {})
        self.assertEqual(assistant_chunks, [])
        self.assertTrue(
            self.service._memory_chunk_is_dirty(
                late_chunk,
                dirty_episode_ids=set(),
                dirty_message_ids=dirty_message_ids,
            )
        )
        self.assertIn("msg_direct_dirty", context)
        self.assertIn("direct source message changed", context)
        self.assertNotIn("direct dirty summary", context)
        self.service.app_state.set_value(self.service._source_dirty_key("oc_dirty_direct"), "[]")
        self.assertEqual(self.service.get_recent_task_changes("oc_dirty_direct"), [])


if __name__ == "__main__":
    unittest.main()
