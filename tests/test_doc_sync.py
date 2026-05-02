import json
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.db.models import Task
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.interaction import InteractionService
from app.services.doc_tool import DocTool, DocumentSyncResult
from app.services.office_artifact_service import OfficeArtifactService
from app.services.session_document_service import SessionDocumentService


class _MemoryStateService:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class DocSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = FeishuWorkflowService()
        self.local_artifact_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.local_artifact_dir.cleanup)
        self.workflow.office_artifact_service = OfficeArtifactService(root_dir=Path(self.local_artifact_dir.name))
        self.workflow.session_document_service = SessionDocumentService(
            state_service=_MemoryStateService()
        )
        self.interaction = InteractionService()
        self.memory_service = MemoryService()

    def test_interaction_service_detects_doc_request(self) -> None:
        decision = self.interaction.decide("帮我把这轮讨论整理成飞书文档")
        self.assertEqual(decision.mode, "doc")

    def test_document_from_analysis_contains_core_sections(self) -> None:
        analysis = AnalyzeResponse(
            session_id="s1",
            summary="讨论明确了后端和前端分工。",
            tasks=[
                TaskItem(
                    title="后端开发",
                    owner="张三",
                    priority="high",
                    due_date="2026-04-18",
                    status="draft",
                    notes="负责接口联调",
                )
            ],
            risks=["后端时间较紧"],
            next_actions=["张三先完成接口联调"],
            agent_traces=[AgentTrace(agent="planner", summary="ok")],
        )

        package = self.workflow._document_from_analysis(analysis, "把这轮讨论整理成文档")
        self.assertIn("title", package)
        self.assertTrue(package["sections"])
        headings = [section["heading"] for section in package["sections"]]
        self.assertIn("讨论摘要", headings)
        self.assertIn("任务清单", headings)

    def test_default_doc_title_includes_timestamp(self) -> None:
        with patch.object(self.workflow, "_doc_title_timestamp", return_value="2026-04-17 11:30"):
            title = self.workflow._default_doc_title("帮我把这轮讨论整理成文档")
        self.assertIn("统计至2026-04-17 11:30", title)

    def test_default_doc_title_prefers_stats_cutoff_time(self) -> None:
        title = self.workflow._default_doc_title(
            "帮我把这轮讨论整理成文档",
            stats_as_of="2026-04-17 11:34",
        )
        self.assertIn("统计至2026-04-17 11:34", title)

    def test_merge_task_items_keeps_previous_unmatched_tasks(self) -> None:
        current_tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="旧任务",
            )
        ]
        refreshed_tasks = [
            TaskItem(
                title="前端开发",
                owner="张三",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="新任务",
            )
        ]
        merged = self.workflow._merge_task_items(current_tasks, refreshed_tasks)
        self.assertEqual(len(merged), 2)
        self.assertTrue(any(task.title == "后端开发" and task.owner == "张三" for task in merged))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "张三" for task in merged))

    def test_update_current_tasks_from_discussion_prefers_explicit_new_assignment(self) -> None:
        current_tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="张三4月30号之前搞定后端",
            ),
            TaskItem(
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="李四4月29号之前搞定前端",
            ),
        ]
        updated = self.workflow._update_current_tasks_from_discussion(
            current_tasks,
            "张三你也去搞前端吧",
            llm_tasks=[],
        )
        self.assertTrue(any(task.title == "后端开发" and task.owner == "张三" for task in updated))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "李四" for task in updated))
        self.assertTrue(any(task.title == "前端开发" and task.owner == "张三" for task in updated))

    def test_merge_current_tasks_can_drop_removed_items_when_snapshot_is_exact(self) -> None:
        previous_tasks = [
            Task(
                id=1,
                session_id="s1",
                title="后端开发",
                owner="张三",
                priority="high",
                due_date="2026-04-30",
                status="draft",
                notes="旧任务",
            ),
            Task(
                id=2,
                session_id="s1",
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="保留任务",
            ),
        ]
        current_tasks = [
            TaskItem(
                title="前端开发",
                owner="李四",
                priority="medium",
                due_date="2026-04-29",
                status="draft",
                notes="保留任务",
            )
        ]

        merged = self.memory_service._merge_current_tasks(
            previous_tasks,
            current_tasks,
            preserve_unmatched_previous=False,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "前端开发")

    def test_prepare_doc_execution_includes_current_document_sync_preview(self) -> None:
        session_id = "doc_session_preview"
        self.workflow.session_document_service.clear_current_document(session_id)
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": session_id,
                "message_id": "m_doc_1",
                "text": "请整理成文档",
            },
        )()
        package = {
            "title": "协作文档",
            "stats_as_of": "2026-04-28 10:00",
            "sections": [
                {
                    "heading": "讨论摘要",
                    "paragraphs": ["整理一版可继续协作的需求文档"],
                }
            ],
        }

        with patch.object(
            self.workflow,
            "_build_doc_response_package",
            return_value=(package, None),
        ), patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "create_document_from_sections",
            return_value={
                "document_id": "doc_123",
                "url": "https://feishu.cn/docx/doc_123",
                "title": "协作文档",
                "folder_scope": "none",
                "folder_url": None,
                "folder_note": None,
            },
        ):
            result = self.workflow._prepare_doc_execution(
                message,
                llm_result={},
                workspace_context="workspace",
                active_episode_id=None,
                reason="生成协作文档",
            )

        artifact = result["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "document")
        self.assertEqual(artifact["provider"], "feishu_doc")
        self.assertEqual(artifact["url"], "https://feishu.cn/docx/doc_123")
        self.assertEqual(artifact["version"], 1)
        self.assertIn("sync", artifact["preview"])
        self.assertEqual(artifact["preview"]["sync"]["mode"], "created")
        self.assertEqual(artifact["preview"]["sync"]["version"], 1)
        self.assertEqual(
            artifact["preview"]["sync"]["url"],
            "https://feishu.cn/docx/doc_123",
        )

    def test_disabled_doc_sync_creates_local_ready_artifact(self) -> None:
        session_id = "doc_session_local_ready"
        self.workflow.session_document_service.clear_current_document(session_id)
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": session_id,
                "message_id": "m_doc_local",
                "text": "write a project note",
            },
        )()
        package = {
            "title": "Project Note",
            "stats_as_of": "2026-04-29 17:30",
            "sections": [{"heading": "Summary", "paragraphs": ["Local artifact is available."]}],
        }

        with patch.object(
            self.workflow,
            "_build_doc_response_package",
            return_value=(package, None),
        ), patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=False,
        ):
            result = self.workflow._prepare_doc_execution(
                message,
                llm_result={},
                workspace_context="workspace",
                active_episode_id=None,
                reason="doc",
                task_run_id="run_local_ready",
            )

        artifact = result["artifacts"][0]
        self.assertEqual(artifact["status"], "local_ready")
        self.assertEqual(artifact["provider"], "local")
        self.assertEqual(artifact["url"], "/api/artifacts/doc/run_local_ready.md")
        self.assertEqual(artifact["preview"]["sync"]["mode"], "local_only")
        self.assertEqual(artifact["preview"]["sync"]["status"], "local_ready")
        local_path = Path(self.local_artifact_dir.name) / "doc" / "run_local_ready.md"
        self.assertTrue(local_path.is_file())
        self.assertIn("Local artifact is available.", local_path.read_text(encoding="utf-8"))

    def test_failed_doc_update_artifact_does_not_reuse_previous_document_url(self) -> None:
        session_id = "doc_session_failed_update"
        self.workflow.session_document_service.save_current_document(
            session_id,
            document_id="doc_old",
            url="https://feishu.cn/docx/doc_old",
            title="旧协作文档",
            version=2,
            sync_mode="updated",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["旧内容"]},
            ],
        )
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": session_id,
                "message_id": "m_doc_fail",
                "text": "更新文档",
            },
        )()
        package = {
            "title": "协作文档",
            "stats_as_of": "2026-04-29 16:20",
            "sections": [
                {
                    "heading": "讨论摘要",
                    "paragraphs": ["更新后的内容"],
                }
            ],
        }

        with patch.object(
            self.workflow,
            "_build_doc_response_package",
            return_value=(package, None),
        ), patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            side_effect=RuntimeError("replace down"),
        ), patch.object(
            self.workflow.doc_api,
            "create_document_from_sections",
            side_effect=RuntimeError("create down"),
        ):
            result = self.workflow._prepare_doc_execution(
                message,
                llm_result={},
                workspace_context="workspace",
                active_episode_id=None,
                reason="更新文档",
            )

        artifact = result["artifacts"][0]
        self.assertEqual(artifact["status"], "sync_failed")
        self.assertEqual(artifact["provider"], "local")
        self.assertTrue(str(artifact["url"]).startswith("/api/artifacts/doc/"))
        self.assertEqual(artifact["preview"]["sync"]["mode"], "sync_failed")
        self.assertIn("create down", artifact["preview"]["sync"]["error"])
        self.assertEqual(
            artifact["preview"]["sync"]["previous_document"]["url"],
            "https://feishu.cn/docx/doc_old",
        )
        local_artifact = artifact["preview"]["sync"]["local_artifact"]
        self.assertEqual(local_artifact["url"], artifact["url"])
        self.assertNotIn("path", local_artifact)
        local_path = Path(self.local_artifact_dir.name) / "doc" / local_artifact["filename"]
        self.assertTrue(local_path.is_file())
        self.assertIn("# ", local_path.read_text(encoding="utf-8"))

    def test_doc_response_package_prefers_llm_doc_sections(self) -> None:
        with patch.object(
            self.workflow.memory_service,
            "get_discussion_cutoff_at",
            return_value=None,
        ):
            package, analysis = self.workflow._build_doc_response_package(
                session_id="s1",
                instruction="把当前讨论整理成文档",
                llm_result={
                    "doc": {
                        "title": "评审版需求文档",
                        "sections": [
                            {
                                "heading": "风险",
                                "paragraphs": ["接口联调窗口偏紧，需要提前锁定测试环境。"],
                            }
                        ],
                    }
                },
                workspace_context="旧的会话摘要",
                episode_id=None,
                reason="生成文档",
                source_message_id="m1",
            )

        self.assertIsNone(analysis)
        self.assertEqual(package["title"], "评审版需求文档")
        self.assertEqual(package["sections"][0]["heading"], "风险与卡点")
        self.assertEqual(
            package["sections"][0]["paragraphs"],
            ["接口联调窗口偏紧，需要提前锁定测试环境。"],
        )

    def test_revision_context_includes_current_document_snapshot(self) -> None:
        context = self.workflow._format_current_document_context(
            {
                "document_id": "doc_1",
                "title": "协作文档",
                "version": 2,
                "url": "https://feishu.cn/docx/doc_1",
                "section_snapshot": [
                    {
                        "heading": "风险与卡点",
                        "paragraphs": ["接口联调时间偏紧", "第三方依赖待确认"],
                    }
                ],
            }
        )

        self.assertIn("[当前协作文档]", context)
        self.assertIn("标题：协作文档", context)
        self.assertIn("版本：v2", context)
        self.assertIn("- 风险与卡点", context)
        self.assertIn("接口联调时间偏紧", context)

    def test_im_doc_update_context_includes_current_doc_snapshot_and_latest_discussion(self) -> None:
        session_id = "doc_update_context"
        self.workflow.session_document_service.save_current_document(
            session_id,
            document_id="doc_1",
            url="https://feishu.cn/docx/doc_1",
            title="协作文档",
            version=2,
            section_snapshot=[
                {
                    "heading": "任务清单",
                    "paragraphs": ["1. 后端开发｜负责人：张三｜截止：2026-04-30｜优先级：high"],
                }
            ],
        )
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": session_id,
                "message_id": "m_update",
                "text": "你来更新一下文档",
            },
        )()

        with patch.object(
            self.workflow.memory_service,
            "build_discussion_block",
            return_value="[近期群聊讨论]\n- 张三：后端的话，打算本周六执行完毕吧",
        ):
            context = self.workflow._build_doc_update_context(
                message,
                "[协作上下文]",
                active_episode_id=9,
            )

        self.assertIn("[当前协作文档]", context)
        self.assertIn("标题：协作文档", context)
        self.assertIn("截止：2026-04-30", context)
        self.assertIn("[本次待同步讨论]", context)
        self.assertIn("后端的话，打算本周六执行完毕吧", context)
        self.assertIn("[文档更新策略]", context)

    def test_session_doc_sync_records_latest_task_run_id(self) -> None:
        session_id = "doc_session_trace"
        self.workflow.session_document_service.clear_current_document(session_id)
        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "create_document_from_sections",
            return_value={
                "document_id": "doc_trace",
                "url": "https://feishu.cn/docx/doc_trace",
                "title": "追踪文档",
                "folder_scope": "none",
                "folder_url": None,
                "folder_note": None,
            },
        ):
            self.workflow._sync_package_to_session_doc(
                {
                    "title": "追踪文档",
                    "sections": [{"heading": "讨论摘要", "paragraphs": ["一版内容"]}],
                },
                session_id=session_id,
                episode_id=None,
                instruction="生成文档",
                task_run_id="run_doc_1",
            )

        current_doc = self.workflow.session_document_service.get_current_document(session_id)
        self.assertIsNotNone(current_doc)
        self.assertEqual(current_doc["task_run_id"], "run_doc_1")

    def test_session_document_service_tracks_document_history(self) -> None:
        session_service = SessionDocumentService(state_service=_MemoryStateService())
        session_service.save_current_document(
            "history_session",
            document_id="doc_a",
            url="https://feishu.cn/docx/doc_a",
            title="文档 A",
            version=1,
            sync_mode="created",
            section_snapshot=[{"heading": "讨论摘要", "paragraphs": ["A"]}],
        )
        session_service.save_current_document(
            "history_session",
            document_id="doc_b",
            url="https://feishu.cn/docx/doc_b",
            title="文档 B",
            version=1,
            sync_mode="created",
            section_snapshot=[{"heading": "讨论摘要", "paragraphs": ["B"]}],
        )

        documents = session_service.list_documents("history_session")
        self.assertEqual([item["document_id"] for item in documents], ["doc_b", "doc_a"])
        self.assertTrue(documents[0]["is_current"])
        self.assertFalse(documents[1]["is_current"])
        selected = session_service.get_document("history_session", "doc_a")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["title"], "文档 A")

    def test_session_document_service_tolerates_non_numeric_version(self) -> None:
        session_service = SessionDocumentService(state_service=_MemoryStateService())
        document = session_service.save_current_document(
            "version_session",
            document_id="doc_a",
            url="https://feishu.cn/docx/doc_a",
            title="文档 A",
            version="draft",
            sync_mode="created",
        )

        self.assertEqual(document["version"], 1)

    def test_document_sync_result_tolerates_non_numeric_version(self) -> None:
        result = DocumentSyncResult(
            mode="updated",
            status="ready",
            summary_lines=[],
            document_info={"version": "draft"},
        )

        self.assertEqual(result.version, 1)

    def test_session_document_service_orders_current_first_then_recent_history(self) -> None:
        state_service = _MemoryStateService()
        session_service = SessionDocumentService(state_service=state_service)
        documents = [
            {
                "session_id": "history_session",
                "document_id": "doc_old",
                "url": "https://feishu.cn/docx/doc_old",
                "title": "旧文档",
                "version": 1,
                "sync_mode": "created",
                "section_snapshot": [],
                "section_block_index": [],
                "updated_at": "2026-04-29T08:00:00+00:00",
            },
            {
                "session_id": "history_session",
                "document_id": "doc_current",
                "url": "https://feishu.cn/docx/doc_current",
                "title": "当前文档",
                "version": 3,
                "sync_mode": "updated",
                "section_snapshot": [],
                "section_block_index": [],
                "updated_at": "2026-04-29T09:00:00+00:00",
            },
            {
                "session_id": "history_session",
                "document_id": "doc_recent",
                "url": "https://feishu.cn/docx/doc_recent",
                "title": "较新历史文档",
                "version": 2,
                "sync_mode": "updated",
                "section_snapshot": [],
                "section_block_index": [],
                "updated_at": "2026-04-29T10:00:00+00:00",
            },
        ]
        state_service.set_value("session_docs:history_session", json.dumps(documents, ensure_ascii=False))
        state_service.set_value("session_doc:history_session", json.dumps(documents[1], ensure_ascii=False))

        ordered = session_service.list_documents("history_session")

        self.assertEqual([item["document_id"] for item in ordered], ["doc_current", "doc_recent", "doc_old"])
        self.assertTrue(ordered[0]["is_current"])

    def test_doc_tool_syncs_package_and_records_current_document(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )
        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "create_document_from_sections",
            return_value={
                "document_id": "doc_tool_1",
                "url": "https://feishu.cn/docx/doc_tool_1",
                "title": "工具文档",
                "folder_scope": "none",
                "folder_url": None,
                "folder_note": None,
            },
        ):
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "工具文档",
                    "sections": [{"heading": "摘要", "paragraphs": ["工具边界"]}],
                },
                session_id="doc_tool_session",
                episode_id=None,
                instruction="生成文档",
                task_run_id="run_doc_tool",
            )

        current_doc = session_document_service.get_current_document("doc_tool_session")
        self.assertEqual(result.mode, "created")
        self.assertEqual(result.url, "https://feishu.cn/docx/doc_tool_1")
        self.assertIsNotNone(current_doc)
        self.assertEqual(current_doc["task_run_id"], "run_doc_tool")
        self.assertEqual(current_doc["section_snapshot"][0]["heading"], "讨论摘要")

    def test_doc_tool_replaces_changed_sections_in_existing_document(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_replace_session",
            document_id="doc_replace",
            url="https://feishu.cn/docx/doc_replace",
            title="替换文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "风险与卡点", "paragraphs": ["旧风险"]},
            ],
            section_block_index=[
                {"heading": "讨论摘要", "start_index": 0, "end_index": 2, "block_ids": ["h1", "p1"]},
                {"heading": "风险与卡点", "start_index": 2, "end_index": 4, "block_ids": ["h2", "p2"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_replace",
                "url": "https://feishu.cn/docx/doc_replace",
                "title": "替换文档",
                "replaced_block_count": 2,
                "inserted_block_count": 2,
                "replaced_headings": ["风险与卡点"],
                "appended_headings": [],
                "section_block_index": [
                    {"heading": "讨论摘要", "start_index": 0, "end_index": 2, "block_ids": ["h1", "p1"]},
                    {"heading": "风险与卡点", "start_index": 2, "end_index": 4, "block_ids": ["h3", "p3"]},
                ],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "替换文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                        {"heading": "风险与卡点", "paragraphs": ["新风险"]},
                    ],
                },
                session_id="doc_replace_session",
                episode_id=None,
                instruction="补充一下风险部分",
                task_run_id="run_replace",
            )

        replace_sections.assert_called_once()
        _, _, sections = replace_sections.call_args.args
        self.assertEqual([section["heading"] for section in sections], ["讨论摘要", "风险与卡点"])
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["风险与卡点"])
        self.assertEqual(result.mode, "updated")
        self.assertTrue(any("Replaced blocks: 2" in line for line in result.summary_lines))
        current_doc = session_document_service.get_current_document("doc_replace_session")
        self.assertIsNotNone(current_doc)
        self.assertEqual(current_doc["section_block_index"][1]["block_ids"], ["h3", "p3"])

    def test_doc_tool_snapshot_only_merges_written_sections(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_target_snapshot_session",
            document_id="doc_target_snapshot",
            url="https://feishu.cn/docx/doc_target_snapshot",
            title="目标更新文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "风险与卡点", "paragraphs": ["旧风险"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_target_snapshot",
                "url": "https://feishu.cn/docx/doc_target_snapshot",
                "title": "目标更新文档",
                "replaced_block_count": 1,
                "inserted_block_count": 1,
                "patched_headings": ["风险与卡点"],
                "section_block_index": [],
            },
        ):
            doc_tool.sync_package_to_session_doc(
                {
                    "title": "目标更新文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["LLM 误改摘要"]},
                        {"heading": "风险与卡点", "paragraphs": ["新风险"]},
                    ],
                },
                session_id="doc_target_snapshot_session",
                episode_id=None,
                instruction="更新风险部分",
                task_run_id="run_target_snapshot",
            )

        current_doc = session_document_service.get_current_document("doc_target_snapshot_session")
        self.assertIsNotNone(current_doc)
        self.assertEqual(
            current_doc["section_snapshot"],
            [
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "风险与卡点", "paragraphs": ["新风险"]},
            ],
        )

    def test_doc_tool_passes_delete_and_rename_plan_to_doc_api(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_plan_session",
            document_id="doc_plan",
            url="https://feishu.cn/docx/doc_plan",
            title="Planning Doc",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "Summary", "paragraphs": ["Old summary"]},
                {"heading": "Risks", "paragraphs": ["Old risk"]},
                {"heading": "Next Steps", "paragraphs": ["Old next"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_plan",
                "url": "https://feishu.cn/docx/doc_plan",
                "title": "Planning Doc",
                "replaced_block_count": 1,
                "inserted_block_count": 1,
                "replaced_headings": ["Key Risks"],
                "appended_headings": [],
                "deleted_headings": ["Next Steps"],
                "renamed_headings": ["Risks -> Key Risks"],
                "patched_headings": ["Key Risks"],
                "section_block_index": [],
            },
        ) as replace_sections:
            doc_tool.sync_package_to_session_doc(
                {
                    "title": "Planning Doc",
                    "sections": [
                        {"heading": "Summary", "paragraphs": ["Fresh summary"]},
                        {"heading": "Key Risks", "paragraphs": ["Risk A"]},
                    ],
                },
                session_id="doc_plan_session",
                episode_id=None,
                instruction="Please rename Risks to Key Risks and delete Next Steps.",
                task_run_id="run_plan",
            )

        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], ["Next Steps"])
        self.assertEqual(replace_sections.call_args.kwargs["rename_map"], {"Risks": "Key Risks"})
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["Key Risks"])

    def test_doc_tool_rewrites_same_content_for_formatting_request(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_format_session",
            document_id="doc_format",
            url="https://feishu.cn/docx/doc_format",
            title="格式文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["已有摘要"]},
                {"heading": "任务清单", "paragraphs": ["已有任务"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_format",
                "url": "https://feishu.cn/docx/doc_format",
                "title": "格式文档",
                "replaced_block_count": 2,
                "inserted_block_count": 2,
                "patched_headings": ["讨论摘要", "任务清单"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "格式文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["已有摘要"]},
                        {"heading": "任务清单", "paragraphs": ["已有任务"]},
                    ],
                },
                session_id="doc_format_session",
                episode_id=None,
                instruction="你能否来帮我整理一下文档，使文档的格式更加规范",
                task_run_id="run_format",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["讨论摘要", "任务清单"])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_deletes_explicit_update_heading_from_natural_language(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_delete_session",
            document_id="doc_delete",
            url="https://feishu.cn/docx/doc_delete",
            title="删除文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["已有摘要"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_delete",
                "url": "https://feishu.cn/docx/doc_delete",
                "title": "删除文档",
                "replaced_block_count": 2,
                "inserted_block_count": 0,
                "deleted_headings": ["Update (2026-04-29 15:43)"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "删除文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["已有摘要"]},
                        {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
                    ],
                },
                session_id="doc_delete_session",
                episode_id=None,
                instruction="帮我删除Update (2026-04-29 15:43)栏一下的内容",
                task_run_id="run_delete",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], [])
        self.assertEqual(replace_sections.call_args.kwargs["delete_ranges"][0]["scope"], "group")
        self.assertEqual(replace_sections.call_args.kwargs["delete_ranges"][0]["anchor"], "Update (2026-04-29 15:43)")
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], [])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_delete_only_plan_does_not_patch_llm_sections(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_delete_only_session",
            document_id="doc_delete_only",
            url="https://feishu.cn/docx/doc_delete_only",
            title="删除文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["已有摘要"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
                {"heading": "下一步建议", "paragraphs": ["已有建议"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_delete_only",
                "url": "https://feishu.cn/docx/doc_delete_only",
                "title": "删除文档",
                "replaced_block_count": 2,
                "inserted_block_count": 0,
                "deleted_headings": ["Update (2026-04-29 15:43)"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "删除文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["LLM 误改摘要"]},
                        {"heading": "下一步建议", "paragraphs": ["LLM 误改建议"]},
                        {"heading": "建议补充素材", "paragraphs": ["LLM 误加内容"]},
                    ],
                },
                session_id="doc_delete_only_session",
                episode_id=None,
                instruction="帮我删除Update (2026-04-29 15:43)栏以下的内容",
                task_run_id="run_delete_only",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.args[2], [])
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], [])
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], [])
        self.assertEqual(
            replace_sections.call_args.kwargs["delete_ranges"],
            [
                {
                    "scope": "group",
                    "anchor": "Update (2026-04-29 15:43)",
                    "query": "Update (2026-04-29 15:43)",
                    "include_anchor": True,
                    "stop_at": "",
                    "label": "Update (2026-04-29 15:43) 这一组",
                }
            ],
        )
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_reports_unmatched_delete_range_without_fake_update(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_unmatched_delete_range_session",
            document_id="doc_unmatched_delete_range",
            url="https://feishu.cn/docx/doc_unmatched_delete_range",
            title="删除文档",
            version=9,
            sync_mode="created",
            section_snapshot=[
                {"heading": "项目背景", "paragraphs": ["保留"]},
                {"heading": "后续计划", "paragraphs": ["保留计划"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_unmatched_delete_range",
                "url": "https://feishu.cn/docx/doc_unmatched_delete_range",
                "title": "删除文档",
                "replaced_block_count": 0,
                "inserted_block_count": 0,
                "deleted_headings": [],
                "unmatched_delete_ranges": ["后续计划 之后"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "删除文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["LLM 误生成摘要"]},
                    ],
                    "artifact_edit_plan": {
                        "ops": [
                            {
                                "type": "delete",
                                "target": {
                                    "kind": "anchor_range",
                                    "anchor": "后续计划",
                                    "query": "后续计划",
                                    "scope": "after",
                                    "include_anchor": False,
                                },
                            }
                        ]
                    },
                },
                session_id="doc_unmatched_delete_range_session",
                episode_id=None,
                instruction="将文档里“后续计划”后面的内容全部删除",
                task_run_id="run_unmatched_delete_range",
            )

        replace_sections.assert_called_once()
        self.assertEqual(result.mode, "noop")
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("- Operation targets not matched: 删除：后续计划 之后", result.summary_lines)
        self.assertEqual(result.document_info["version"], 9)
        self.assertEqual(
            result.document_info["section_snapshot"],
            [
                {"heading": "项目背景", "paragraphs": ["保留"]},
                {"heading": "后续计划", "paragraphs": ["保留计划"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
            ],
        )

    def test_doc_tool_reports_unmatched_update_target_without_fake_append(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_unmatched_update_session",
            document_id="doc_unmatched_update",
            url="https://feishu.cn/docx/doc_unmatched_update",
            title="更新文档",
            version=4,
            sync_mode="created",
            section_snapshot=[
                {"heading": "项目背景", "paragraphs": ["保留"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_unmatched_update",
                "url": "https://feishu.cn/docx/doc_unmatched_update",
                "title": "更新文档",
                "replaced_block_count": 0,
                "inserted_block_count": 0,
                "unmatched_update_headings": ["后续计划"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "更新文档",
                    "sections": [{"heading": "后续计划", "paragraphs": ["新计划"]}],
                    "artifact_edit_plan": {
                        "operations": [
                            {
                                "type": "update",
                                "target": {"kind": "heading", "query": "后续计划"},
                            }
                        ]
                    },
                },
                session_id="doc_unmatched_update_session",
                episode_id=None,
                instruction="更新文档里的后续计划",
                task_run_id="run_unmatched_update",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["append_headings"], [])
        self.assertEqual(result.mode, "noop")
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("- Operation targets not matched: 更新：后续计划", result.summary_lines)
        self.assertEqual(result.document_info["version"], 4)

    def test_doc_tool_multi_intent_delete_and_rewrite_keeps_both_operations(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_multi_intent_session",
            document_id="doc_multi_intent",
            url="https://feishu.cn/docx/doc_multi_intent",
            title="多意图文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["旧更新"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_multi_intent",
                "url": "https://feishu.cn/docx/doc_multi_intent",
                "title": "多意图文档",
                "replaced_block_count": 3,
                "inserted_block_count": 1,
                "deleted_headings": ["Update (2026-04-29 15:43)"],
                "patched_headings": ["讨论摘要"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "多意图文档",
                    "sections": [
                        {"heading": "讨论摘要", "paragraphs": ["新摘要"]},
                    ],
                },
                session_id="doc_multi_intent_session",
                episode_id=None,
                instruction="删除 Update (2026-04-29 15:43) 栏，然后重新总结一下文档",
                task_run_id="run_multi_intent",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], [])
        self.assertEqual(replace_sections.call_args.kwargs["delete_ranges"][0]["scope"], "group")
        self.assertEqual(replace_sections.call_args.kwargs["delete_ranges"][0]["anchor"], "Update (2026-04-29 15:43)")
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["讨论摘要"])
        self.assertEqual(replace_sections.call_args.args[2], [{"heading": "讨论摘要", "paragraphs": ["新摘要"]}])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_structured_delete_plan_does_not_need_delete_keyword(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_structured_delete_session",
            document_id="doc_structured_delete",
            url="https://feishu.cn/docx/doc_structured_delete",
            title="Structured Doc",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "Summary", "paragraphs": ["Keep summary"]},
                {"heading": "Old Section", "paragraphs": ["Remove me"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_structured_delete",
                "url": "https://feishu.cn/docx/doc_structured_delete",
                "title": "Structured Doc",
                "deleted_headings": ["Old Section"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "Structured Doc",
                    "sections": [
                        {"heading": "Summary", "paragraphs": ["LLM should not be written"]},
                        {"heading": "New Hallucinated Section", "paragraphs": ["Skip me"]},
                    ],
                    "artifact_edit_plan": {
                        "artifact_type": "doc",
                        "mutation_required": True,
                        "ops": [
                            {
                                "type": "delete",
                                "target": {"kind": "heading", "queries": ["Old Section"]},
                            }
                        ],
                    },
                },
                session_id="doc_structured_delete_session",
                episode_id=None,
                instruction="Apply the approved edit plan.",
                task_run_id="run_structured_delete",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.args[2], [])
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], ["Old Section"])
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], [])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_passes_untracked_structured_delete_target_to_doc_api(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_untracked_delete_session",
            document_id="doc_untracked_delete",
            url="https://feishu.cn/docx/doc_untracked_delete",
            title="Structured Doc",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "Summary", "paragraphs": ["Keep summary"]},
                {"heading": "Next Steps", "paragraphs": ["Keep next"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_untracked_delete",
                "url": "https://feishu.cn/docx/doc_untracked_delete",
                "title": "Structured Doc",
                "deleted_headings": ["Update (2026-04-29 15:43)"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "Structured Doc",
                    "sections": [
                        {"heading": "Summary", "paragraphs": ["Keep summary"]},
                        {"heading": "Next Steps", "paragraphs": ["Keep next"]},
                    ],
                    "artifact_edit_plan": {
                        "artifact_type": "doc",
                        "mutation_required": True,
                        "operations": [
                            {
                                "type": "delete",
                                "target": {"kind": "heading", "queries": ["Update (2026-04-29 15:43)"]},
                            }
                        ],
                    },
                },
                session_id="doc_untracked_delete_session",
                episode_id=None,
                instruction="Delete the old update group.",
                task_run_id="run_untracked_delete",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.args[2], [])
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], [])
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], ["Update (2026-04-29 15:43)"])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_structured_rename_plan_does_not_need_rename_keyword(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_structured_rename_session",
            document_id="doc_structured_rename",
            url="https://feishu.cn/docx/doc_structured_rename",
            title="Structured Doc",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "Summary", "paragraphs": ["Keep summary"]},
                {"heading": "Risks", "paragraphs": ["Old risk"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_structured_rename",
                "url": "https://feishu.cn/docx/doc_structured_rename",
                "title": "Structured Doc",
                "renamed_headings": ["Risks -> Key Risks"],
                "patched_headings": ["Key Risks"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "Structured Doc",
                    "sections": [
                        {"heading": "Summary", "paragraphs": ["Keep summary"]},
                        {"heading": "Key Risks", "paragraphs": ["Risk A"]},
                    ],
                    "artifact_edit_plan": {
                        "artifact_type": "doc",
                        "mutation_required": True,
                        "ops": [
                            {
                                "type": "rename",
                                "target": {"kind": "heading", "query": "Risks"},
                                "payload": {"new_heading": "Key Risks"},
                            }
                        ],
                    },
                },
                session_id="doc_structured_rename_session",
                episode_id=None,
                instruction="Apply the approved edit plan.",
                task_run_id="run_structured_rename",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["rename_map"], {"Risks": "Key Risks"})
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["Key Risks"])
        self.assertEqual(result.mode, "updated")

    def test_doc_tool_structured_append_and_delete_plan_keeps_both_operations(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_structured_combo_session",
            document_id="doc_structured_combo",
            url="https://feishu.cn/docx/doc_structured_combo",
            title="Structured Doc",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "Summary", "paragraphs": ["Keep summary"]},
                {"heading": "Old Section", "paragraphs": ["Remove me"]},
            ],
        )
        doc_tool = DocTool(
            doc_api=self.workflow.doc_api,
            session_document_service=session_document_service,
        )

        with patch.object(
            self.workflow.doc_api,
            "is_configured",
            return_value=True,
        ), patch.object(
            self.workflow.doc_api,
            "replace_document_sections",
            return_value={
                "document_id": "doc_structured_combo",
                "url": "https://feishu.cn/docx/doc_structured_combo",
                "title": "Structured Doc",
                "deleted_headings": ["Old Section"],
                "appended_headings": ["New Section"],
                "patched_headings": ["New Section"],
                "section_block_index": [],
            },
        ) as replace_sections:
            result = doc_tool.sync_package_to_session_doc(
                {
                    "title": "Structured Doc",
                    "sections": [
                        {"heading": "Summary", "paragraphs": ["Keep summary"]},
                        {"heading": "New Section", "paragraphs": ["Added context"]},
                    ],
                    "artifact_edit_plan": {
                        "artifact_type": "doc",
                        "mutation_required": True,
                        "ops": [
                            {
                                "type": "delete",
                                "target": {"kind": "heading", "queries": ["Old Section"]},
                            },
                            {
                                "type": "append",
                                "target": {"kind": "heading", "queries": ["New Section"]},
                            },
                        ],
                    },
                },
                session_id="doc_structured_combo_session",
                episode_id=None,
                instruction="Apply the approved edit plan.",
                task_run_id="run_structured_combo",
            )

        replace_sections.assert_called_once()
        self.assertEqual(replace_sections.call_args.kwargs["delete_headings"], ["Old Section"])
        self.assertEqual(replace_sections.call_args.kwargs["target_headings"], ["New Section"])
        self.assertEqual(
            replace_sections.call_args.args[2],
            [
                {"heading": "Summary", "paragraphs": ["Keep summary"]},
                {"heading": "New Section", "paragraphs": ["Added context"]},
            ],
        )
        self.assertEqual(result.mode, "updated")

    def test_incremental_doc_sections_can_target_risk_section_only(self) -> None:
        package = {
            "stats_as_of": "2026-04-28 11:20",
            "sections": [
                {"heading": "讨论摘要", "paragraphs": ["整体方案已经明确"]},
                {"heading": "风险与卡点", "paragraphs": ["接口联调时间偏紧"]},
                {"heading": "下一步建议", "paragraphs": ["今天完成接口联调排期"]},
            ],
        }
        previous_snapshot = [
            {"heading": "讨论摘要", "paragraphs": ["旧的摘要"]},
            {"heading": "风险与卡点", "paragraphs": ["旧的风险"]},
            {"heading": "下一步建议", "paragraphs": ["旧的建议"]},
        ]

        update_sections, changed_headings, targeted_headings = (
            self.workflow._build_incremental_doc_sections(
                package,
                instruction="补充一下风险部分",
                previous_snapshot=previous_snapshot,
            )
        )

        self.assertEqual(targeted_headings, ["风险与卡点"])
        self.assertEqual(changed_headings, ["风险与卡点"])
        self.assertEqual(len(update_sections), 2)
        self.assertEqual(update_sections[1]["heading"], "refresh: 风险与卡点")

    def test_build_doc_sync_lines_include_updated_section_names(self) -> None:
        lines = self.workflow._build_doc_sync_lines(
            "updated",
            {"title": "协作文档", "url": "https://feishu.cn/docx/doc_1", "version": 3},
            appended_block_count=4,
            changed_headings=["风险与卡点", "任务清单"],
        )

        self.assertTrue(any("Updated sections: 风险与卡点, 任务清单" in line for line in lines))
        self.assertTrue(any("Update strategy: patched matched section bodies" in line for line in lines))

    def test_updated_doc_sync_preview_exposes_replace_strategy(self) -> None:
        preview = self.workflow._build_document_sync_preview(
            {"document_id": "doc_1", "title": "协作文档", "version": 3, "sync_mode": "updated"},
            url="https://feishu.cn/docx/doc_1",
            sync_lines=["- Updated document: 协作文档"],
        )

        self.assertEqual(preview["mode"], "updated")
        self.assertEqual(preview["write_strategy"], "patch_matched_section_bodies")
        self.assertIn("patching matched section bodies", preview["strategy_note"])

    def test_doc_reply_describes_delete_action_without_body_preview(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [{"heading": "讨论摘要", "paragraphs": ["不应该在删除回执里展开"]}],
                "artifact_edit_plan": {
                    "operations": [
                        {
                            "type": "delete",
                            "target": {"queries": ["Update (2026-04-29 15:43)"]},
                        }
                    ]
                },
            },
            [
                "- Updated document: 项目分工文档",
                "- Deleted sections: Update (2026-04-29 15:43)",
                "- Current version: v10",
                "- Document URL: https://feishu.cn/docx/doc_1",
            ],
        )

        self.assertIn("本轮任务：", reply)
        self.assertIn("- 删除：Update (2026-04-29 15:43)", reply)
        self.assertIn("- 已删除：Update (2026-04-29 15:43)", reply)
        self.assertIn("- 当前版本：v10", reply)
        self.assertIn("- 链接：https://feishu.cn/docx/doc_1", reply)
        self.assertNotIn("内容预览：", reply)

    def test_doc_reply_explains_noop_result(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [{"heading": "讨论摘要", "paragraphs": ["已有内容"]}],
                "artifact_edit_plan": {
                    "operations": [
                        {
                            "type": "delete",
                            "target": {"queries": ["Update (2026-04-29 15:43)"]},
                        }
                    ]
                },
            },
            [
                "- No content changes detected. Keep current document: 项目分工文档",
                "- Current version: v9",
                "- Document URL: https://feishu.cn/docx/doc_1",
            ],
        )

        self.assertIn("- 删除：Update (2026-04-29 15:43)", reply)
        self.assertIn("- 未检测到可写入变化，当前文档保持不变：项目分工文档", reply)
        self.assertIn("可能原因", reply)
        self.assertNotIn("No content changes detected", reply)

    def test_doc_reply_describes_update_append_and_rename_actions(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [
                    {"heading": "关键风险", "paragraphs": ["风险更新"]},
                    {"heading": "验收标准", "paragraphs": ["新增验收口径"]},
                ],
                "artifact_edit_plan": {
                    "operations": [
                        {
                            "type": "rename",
                            "target": {"queries": ["风险与卡点"]},
                            "payload": {"new_heading": "关键风险"},
                        },
                        {
                            "type": "update",
                            "target": {"queries": ["关键风险"]},
                        },
                        {
                            "type": "append",
                            "payload": {"heading": "验收标准"},
                        },
                    ]
                },
            },
            [
                "- Updated document: 项目分工文档",
                "- Updated sections: 关键风险",
                "- Appended new sections: 验收标准",
                "- Renamed sections: 风险与卡点 -> 关键风险",
                "- Current version: v11",
                "- Document URL: https://feishu.cn/docx/doc_1",
            ],
        )

        self.assertIn("- 重命名：风险与卡点 -> 关键风险", reply)
        self.assertIn("- 更新：关键风险", reply)
        self.assertIn("- 新增：验收标准", reply)
        self.assertIn("- 已更新章节：关键风险", reply)
        self.assertIn("- 已新增章节：验收标准", reply)
        self.assertIn("- 已重命名：风险与卡点 -> 关键风险", reply)
        self.assertNotIn("内容预览：", reply)

    def test_doc_reply_keeps_preview_for_created_document(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [
                    {"heading": "讨论摘要", "paragraphs": ["团队明确了前后端分工。"]},
                ],
            },
            [
                "- Created document: 项目分工文档",
                "- Current version: v1",
                "- Document URL: https://feishu.cn/docx/doc_1",
            ],
        )

        self.assertIn("内容预览：", reply)
        self.assertIn("讨论摘要", reply)
        self.assertIn("团队明确了前后端分工。", reply)

    def test_doc_reply_infers_actions_from_sync_lines_without_edit_plan(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [{"heading": "任务清单", "paragraphs": ["新增任务"]}],
            },
            [
                "- Updated document: 项目分工文档",
                "- Appended new sections: 任务清单",
                "- Patched section bodies: 讨论摘要",
                "- Current version: v12",
            ],
        )

        self.assertIn("本轮任务：", reply)
        self.assertIn("- 新增：任务清单", reply)
        self.assertIn("- 改写正文：讨论摘要", reply)
        self.assertIn("- 已新增章节：任务清单", reply)
        self.assertIn("- 已改写正文：讨论摘要", reply)

    def test_doc_reply_describes_semantic_delete_range(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [],
                "artifact_edit_plan": {
                    "ops": [
                        {
                            "type": "delete",
                            "target": {
                                "kind": "anchor_range",
                                "anchor": "后续计划",
                                "query": "后续计划",
                                "scope": "after",
                                "include_anchor": False,
                            },
                        }
                    ]
                },
            },
            [
                "- Updated document: 项目分工文档",
                "- Deleted sections: 后续计划 之后",
                "- Current version: v12",
            ],
        )

        self.assertIn("- 删除：后续计划 之后", reply)
        self.assertIn("- 已删除：后续计划 之后", reply)

    def test_doc_reply_explains_unmatched_delete_range(self) -> None:
        reply = self.workflow._format_doc_reply(
            {
                "title": "项目分工文档",
                "sections": [],
                "artifact_edit_plan": {
                    "ops": [
                        {
                            "type": "delete",
                            "target": {
                                "kind": "anchor_range",
                                "anchor": "后续计划",
                                "query": "后续计划",
                                "scope": "after",
                                "include_anchor": False,
                            },
                        }
                    ]
                },
            },
            [
                "- No content changes detected. Keep current document: 项目分工文档",
                "- Operation targets not matched: 删除：后续计划 之后",
                "- Current version: v9",
            ],
        )

        self.assertIn("- 删除：后续计划 之后", reply)
        self.assertIn("- 没有匹配到要操作的目标：删除：后续计划 之后。", reply)
        self.assertIn("本轮未对这些目标做写入", reply)
        self.assertIn("- 当前版本：v9", reply)

    def test_plan_doc_section_changes_can_mark_delete_and_rename(self) -> None:
        plan = self.workflow._doc_tool().plan_doc_section_changes(
            [
                {"heading": "Summary", "paragraphs": ["Fresh summary"]},
                {"heading": "Key Risks", "paragraphs": ["Risk A"]},
            ],
            instruction="Please rename Risks to Key Risks and delete Next Steps.",
            previous_snapshot=[
                {"heading": "Summary", "paragraphs": ["Old summary"]},
                {"heading": "Risks", "paragraphs": ["Old risk"]},
                {"heading": "Next Steps", "paragraphs": ["Old next"]},
            ],
        )

        self.assertEqual(plan["rename_map"], {"Risks": "Key Risks"})
        self.assertEqual(plan["deleted_headings"], ["Next Steps"])
        self.assertEqual(plan["changed_headings"], ["Key Risks"])

    def test_plan_doc_section_changes_preserves_delete_after_range(self) -> None:
        plan = self.workflow._doc_tool().plan_doc_section_changes(
            [],
            instruction="将文档里“后续计划”后面的内容全部删除",
            previous_snapshot=[
                {"heading": "项目背景", "paragraphs": ["保留"]},
                {"heading": "后续计划", "paragraphs": ["保留计划"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["删除"]},
            ],
        )

        self.assertEqual(plan["deleted_headings"], [])
        self.assertEqual(
            plan["delete_ranges"],
            [
                {
                    "scope": "after",
                    "anchor": "后续计划",
                    "query": "后续计划",
                    "include_anchor": False,
                    "stop_at": "",
                    "label": "后续计划 之后",
                }
            ],
        )

    def test_merge_doc_section_snapshots_applies_delete_ranges(self) -> None:
        merged = self.workflow._merge_doc_section_snapshots(
            [
                {"heading": "项目背景", "paragraphs": ["保留"]},
                {"heading": "后续计划", "paragraphs": ["保留计划"]},
                {"heading": "Update (2026-04-29 15:43)", "paragraphs": ["删除"]},
            ],
            [],
            delete_ranges=[
                {
                    "scope": "after",
                    "anchor": "后续计划",
                    "include_anchor": False,
                }
            ],
        )

        self.assertEqual(
            merged,
            [
                {"heading": "项目背景", "paragraphs": ["保留"]},
                {"heading": "后续计划", "paragraphs": ["保留计划"]},
            ],
        )

    def test_doc_tool_prefers_remote_section_snapshot_after_replace(self) -> None:
        session_document_service = SessionDocumentService(state_service=_MemoryStateService())
        session_document_service.save_current_document(
            "doc_remote_snapshot_session",
            document_id="doc_remote_snapshot",
            url="https://feishu.cn/docx/doc_remote_snapshot",
            title="项目文档",
            version=1,
            sync_mode="created",
            section_snapshot=[
                {"heading": "任务清单", "paragraphs": ["后端开发 | Zeleous", "前端开发 | zero"]},
                {"heading": "下一步建议", "paragraphs": ["旧建议"]},
            ],
        )
        doc_api = MagicMock()
        doc_api.replace_document_sections.return_value = {
            "document_id": "doc_remote_snapshot",
            "url": "https://feishu.cn/docx/doc_remote_snapshot",
            "title": "项目文档",
            "replaced_block_count": 1,
            "inserted_block_count": 0,
            "deleted_headings": ["后端开发 之后"],
            "section_block_index": [],
            "section_snapshot": [
                {"heading": "任务清单", "paragraphs": ["后端开发 | Zeleous"]},
            ],
        }
        doc_tool = DocTool(doc_api=doc_api, session_document_service=session_document_service)

        result = doc_tool.sync_package_to_session_doc(
            {
                "title": "项目文档",
                "sections": [],
                "artifact_edit_plan": {
                    "ops": [
                        {
                            "type": "delete",
                            "target": {
                                "kind": "anchor_range",
                                "scope": "after",
                                "anchor": "后端开发",
                                "query": "后端开发",
                                "include_anchor": False,
                            },
                        }
                    ]
                },
            },
            session_id="doc_remote_snapshot_session",
            episode_id=None,
            instruction="把后端开发以后的内容删掉",
        )

        self.assertEqual(result.mode, "updated")
        current_doc = session_document_service.get_current_document("doc_remote_snapshot_session")
        self.assertEqual(
            current_doc["section_snapshot"],
            [{"heading": "任务清单", "paragraphs": ["后端开发 | Zeleous"]}],
        )

    def test_status_reads_tasks_from_current_document_snapshot(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_session",
            document_id="doc_status",
            url="https://feishu.cn/docx/doc_status",
            title="项目分工文档",
            version=3,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        "后端开发 | 负责人: Zeleous | 截止: 2026-05-03 | 优先级: medium | 状态: draft",
                    ],
                }
            ],
        )
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_session",
                "message_id": "m_doc_status",
                "text": "后端开发是谁在负责",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)
        reply = self.workflow._format_status_reply(message.text, tasks, {})

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "后端开发")
        self.assertEqual(tasks[0].owner, "Zeleous")
        self.assertIn("【任务负责人】", reply)
        self.assertIn("后端开发：Zeleous", reply)

    def test_status_task_list_uses_document_snapshot_after_deletion(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_deleted_session",
            document_id="doc_status_deleted",
            url="https://feishu.cn/docx/doc_status_deleted",
            title="项目分工文档",
            version=4,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        "后端开发 | 负责人: Zeleous | 截止: 2026-05-03 | 优先级: medium | 状态: draft",
                    ],
                }
            ],
        )
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_deleted_session",
                "message_id": "m_doc_status_deleted",
                "text": "你说说看都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)
        reply = self.workflow._format_status_reply(message.text, tasks, {})

        self.assertIn("【当前任务】", reply)
        self.assertIn("后端开发", reply)
        self.assertIn("Zeleous", reply)
        self.assertNotIn("前端开发", reply)

    def test_status_document_snapshot_ignores_stale_memory_tasks(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_authoritative_session",
            document_id="doc_status_authoritative",
            url="https://feishu.cn/docx/doc_status_authoritative",
            title="Project Tasks",
            version=3,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "Task list",
                    "paragraphs": [
                        "Backend development | owner: Zeleous | due: TBD | priority: medium | status: draft",
                    ],
                }
            ],
        )
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = [
            Task(
                session_id="doc_status_authoritative_session",
                title="Frontend development",
                owner="zero",
                due_date="TBD",
                priority="medium",
                status="draft",
            )
        ]
        memory_service.get_active_episode.return_value = None
        self.workflow.memory_service = memory_service
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_authoritative_session",
                "message_id": "m_doc_status_authoritative",
                "text": "show current tasks",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)

        self.assertEqual([task.title for task in tasks], ["Backend development"])
        memory_service.get_current_tasks.assert_called_once()

    def test_status_merges_memory_tasks_newer_than_document_snapshot(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_fresh_memory_session",
            document_id="doc_status_fresh_memory",
            url="https://feishu.cn/docx/doc_status_fresh_memory",
            title="Project Tasks",
            version=3,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "Task list",
                    "paragraphs": [
                        "Backend development | owner: Zeleous | due: TBD | priority: medium | status: draft",
                    ],
                }
            ],
        )
        current_doc = self.workflow.session_document_service.get_current_document("doc_status_fresh_memory_session")
        current_doc["updated_at"] = "2026-01-01T00:00:00+00:00"
        self.workflow.session_document_service.state_service.set_value(
            "session_doc:doc_status_fresh_memory_session",
            json.dumps(current_doc, ensure_ascii=False),
        )
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = [
            Task(
                session_id="doc_status_fresh_memory_session",
                title="Product coordination",
                owner="Wang Wu",
                due_date="TBD",
                priority="medium",
                status="draft",
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
        ]
        memory_service.get_active_episode.return_value = None
        self.workflow.memory_service = memory_service
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_fresh_memory_session",
                "message_id": "m_doc_status_fresh_memory",
                "text": "show current tasks",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)

        self.assertEqual([task.title for task in tasks], ["Backend development", "Product coordination"])

    def test_status_execution_persists_active_discussion_tasks(self) -> None:
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = []
        memory_service.get_active_episode.return_value = type("Episode", (), {"id": 11})()
        memory_service.get_episode_messages.return_value = [
            type(
                "Message",
                (),
                {"content": "王五来做产品经理，来协调前端和后端的开发"},
            )()
        ]
        memory_service.load_memory_payload.return_value = {}
        self.workflow.memory_service = memory_service
        self.workflow.llm_service = MagicMock()
        self.workflow.llm_service.is_configured.return_value = False
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_persist_pending_session",
                "message_id": "m_doc_status_persist_pending",
                "text": "说一下目前都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        self.workflow._prepare_status_execution(
            message,
            llm_result={},
            active_episode_id=11,
        )

        saved_analysis = memory_service.save_round.call_args.kwargs["analysis"]
        self.assertEqual(saved_analysis.tasks[0].owner, "王五")
        self.assertIn("产品经理", saved_analysis.tasks[0].title)

    def test_status_execution_ignores_llm_status_answer(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_local_answer_session",
            document_id="doc_status_local_answer",
            url="https://feishu.cn/docx/doc_status_local_answer",
            title="Project Tasks",
            version=3,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "Task list",
                    "paragraphs": [
                        "Backend development | owner: Zeleous | due: TBD | priority: medium | status: draft",
                    ],
                }
            ],
        )
        memory_service = MagicMock()
        memory_service.get_active_episode.return_value = None
        memory_service.load_memory_payload.return_value = {}
        self.workflow.memory_service = memory_service
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_local_answer_session",
                "message_id": "m_doc_status_local_answer",
                "text": "show current tasks",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        result = self.workflow._prepare_status_execution(
            message,
            llm_result={"status_answer": "There are 8 tasks."},
        )

        self.assertIn("Backend development", result["reply_preview"])
        self.assertNotIn("8 tasks", result["reply_preview"])

    def test_status_uses_content_column_instead_of_speaker_as_task_title(self) -> None:
        self.workflow.session_document_service.save_current_document(
            "doc_status_speaker_session",
            document_id="doc_status_speaker",
            url="https://feishu.cn/docx/doc_status_speaker",
            title="项目分工文档",
            version=5,
            sync_mode="updated",
            section_snapshot=[
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        "发言人 Zeleous | 内容 帮我整理代办 | 负责人: TBD | 截止: TBD | 优先级: medium | 状态: draft",
                    ],
                }
            ],
        )
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "doc_status_speaker_session",
                "message_id": "m_doc_status_speaker",
                "text": "你说说看都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)
        reply = self.workflow._format_status_reply(message.text, tasks, {})

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "帮我整理代办")
        self.assertNotEqual(tasks[0].title, "发言人 Zeleous")
        self.assertIn("帮我整理代办", reply)
        self.assertNotIn("发言人 Zeleous | 负责人", reply)

    def test_status_reads_tasks_from_pending_unmentioned_discussion(self) -> None:
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = []
        memory_service.get_active_episode.return_value = type("Episode", (), {"id": 11})()
        memory_service.get_episode_messages.return_value = [
            type(
                "Message",
                (),
                {"content": "王五来做产品经理，来协调前端和后端的开发"},
            )()
        ]
        self.workflow.memory_service = memory_service
        self.workflow.llm_service = MagicMock()
        self.workflow.llm_service.is_configured.return_value = False
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "pending_status_session",
                "message_id": "m_pending_status",
                "text": "说一下目前都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)
        reply = self.workflow._format_status_reply(message.text, tasks, {})

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "王五")
        self.assertEqual(tasks[0].title, "产品经理协调前后端开发")
        self.assertIn("产品经理协调前后端开发", reply)
        self.assertIn("王五", reply)

    def test_status_prefers_llm_for_pending_discussion_task_extraction(self) -> None:
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = []
        memory_service.get_active_episode.return_value = type("Episode", (), {"id": 11})()
        memory_service.get_episode_messages.return_value = [
            type(
                "Message",
                (),
                {"content": "王五来做产品经理，来协调前端和后端的开发"},
            )()
        ]
        llm_service = MagicMock()
        llm_service.is_configured.return_value = True
        llm_service.extract_collaboration.return_value = {
            "summary": "新增产品协调分工",
            "tasks": [
                {
                    "title": "产品经理协调前后端开发",
                    "owner": "王五",
                    "priority": "medium",
                    "due_date": "TBD",
                    "status": "draft",
                    "notes": "王五来做产品经理，来协调前端和后端的开发",
                }
            ],
            "risks": [],
            "next_actions": [],
        }
        self.workflow.memory_service = memory_service
        self.workflow.llm_service = llm_service
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "pending_status_llm_session",
                "message_id": "m_pending_status_llm",
                "text": "说一下目前都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)

        llm_service.extract_collaboration.assert_called_once()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "王五")
        self.assertEqual(tasks[0].title, "产品经理协调前后端开发")

    def test_status_falls_back_to_local_pending_task_parser_when_llm_fails(self) -> None:
        memory_service = MagicMock()
        memory_service.get_current_tasks.return_value = []
        memory_service.get_active_episode.return_value = type("Episode", (), {"id": 11})()
        memory_service.get_episode_messages.return_value = [
            type(
                "Message",
                (),
                {"content": "王五来做产品经理，来协调前端和后端的开发"},
            )()
        ]
        llm_service = MagicMock()
        llm_service.is_configured.return_value = True
        llm_service.extract_collaboration.side_effect = RuntimeError("llm down")
        self.workflow.memory_service = memory_service
        self.workflow.llm_service = llm_service
        message = type(
            "FakeMessage",
            (),
            {
                "session_id": "pending_status_llm_fallback_session",
                "message_id": "m_pending_status_llm_fallback",
                "text": "说一下目前都有什么任务",
                "chat_id": "c1",
                "chat_type": "group",
            },
        )()

        tasks = self.workflow._context_tasks_for_message(message)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "王五")
        self.assertEqual(tasks[0].title, "产品经理协调前后端开发")

    def test_merge_doc_section_snapshots_supports_delete_and_rename(self) -> None:
        merged = self.workflow._merge_doc_section_snapshots(
            [
                {"heading": "Summary", "paragraphs": ["Old summary"]},
                {"heading": "Risks", "paragraphs": ["Old risk"]},
                {"heading": "Next Steps", "paragraphs": ["Old next"]},
            ],
            [
                {"heading": "Summary", "paragraphs": ["Fresh summary"]},
                {"heading": "Key Risks", "paragraphs": ["Risk A"]},
            ],
            deleted_headings=["Next Steps"],
            rename_map={"Risks": "Key Risks"},
        )

        self.assertEqual(
            merged,
            [
                {"heading": "Summary", "paragraphs": ["Fresh summary"]},
                {"heading": "Key Risks", "paragraphs": ["Risk A"]},
            ],
        )

    def test_updated_doc_snapshot_merges_changed_sections_with_previous_snapshot(self) -> None:
        previous_snapshot = [
            {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
            {"heading": "任务清单", "paragraphs": ["1. 后端开发｜截止：2026-04-30"]},
            {"heading": "风险与卡点", "paragraphs": ["接口联调时间偏紧"]},
        ]
        updated_sections = [
            {"heading": "任务清单", "paragraphs": ["1. 后端开发｜截止：2026-05-02"]},
        ]

        merged = self.workflow._merge_doc_section_snapshots(previous_snapshot, updated_sections)

        self.assertEqual(
            merged,
            [
                {"heading": "讨论摘要", "paragraphs": ["旧摘要"]},
                {"heading": "任务清单", "paragraphs": ["1. 后端开发｜截止：2026-05-02"]},
                {"heading": "风险与卡点", "paragraphs": ["接口联调时间偏紧"]},
            ],
        )

    def test_normalize_doc_sections_merges_aliases_and_keeps_stable_order(self) -> None:
        sections = [
            {"heading": "风险", "paragraphs": ["接口联调时间紧"]},
            {"heading": "摘要", "paragraphs": ["整体方案已对齐"]},
            {"heading": "任务", "paragraphs": ["1. 张三负责后端"]},
            {"heading": "风险与卡点", "paragraphs": ["接口联调时间紧", "第三方依赖待确认"]},
        ]

        normalized = self.workflow._normalize_doc_sections(sections)

        self.assertEqual(
            [section["heading"] for section in normalized],
            ["讨论摘要", "任务清单", "风险与卡点"],
        )
        risk_section = normalized[2]
        self.assertEqual(
            risk_section["paragraphs"],
            ["接口联调时间紧", "第三方依赖待确认"],
        )

if __name__ == "__main__":
    unittest.main()
