import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.db.models import Task
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.interaction import InteractionService
from app.services.doc_tool import DocTool
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
