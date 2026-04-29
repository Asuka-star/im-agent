import unittest
from unittest.mock import patch

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.db.models import Task
from app.schemas.task import TaskItem
from app.services.memory_service import MemoryService
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.interaction import InteractionService
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
            "append_sections_to_document",
            side_effect=RuntimeError("append down"),
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
        self.assertIsNone(artifact["url"])
        self.assertEqual(artifact["preview"]["sync"]["mode"], "sync_failed")
        self.assertIn("create down", artifact["preview"]["sync"]["error"])
        self.assertEqual(
            artifact["preview"]["sync"]["previous_document"]["url"],
            "https://feishu.cn/docx/doc_old",
        )

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
