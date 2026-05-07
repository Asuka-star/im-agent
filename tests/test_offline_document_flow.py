import hashlib
import unittest
import json
from types import SimpleNamespace

from app.schemas.feishu_event import FeishuMessageContext
from app.services.offline_document_merge import OfflineDocumentMergeService
from app.services.offline_document_parser import OfflineDocumentParser
from app.services.workflow.offline_document_execution import WorkflowOfflineDocumentExecution
from app.services.workflow.revisions import WorkbenchRevisionWorkflow


class _TaskRunService:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.updates: list[dict] = []
        self.metadata: dict = {}
        self.task_runs: dict[str, SimpleNamespace] = {}
        self.task_run_summaries: list[SimpleNamespace] = []
        self.artifact_sources: dict[str, str] = {
            "artifact_slides_1": "run_slides_source",
            "artifact_canvas_1": "run_canvas_source",
        }

    def upsert_step(self, task_run_id: str, **kwargs) -> None:
        self.steps.append({"task_run_id": task_run_id, **kwargs})

    def update_task_run(self, task_run_id: str, **kwargs) -> None:
        self.updates.append({"task_run_id": task_run_id, **kwargs})
        if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
            self.metadata = dict(kwargs["metadata"])

    def create_confirmation(self, task_run_id: str, *, prompt: str, options: list[str] | None = None):
        self.confirmation = {"task_run_id": task_run_id, "prompt": prompt, "options": list(options or [])}
        return SimpleNamespace(confirmation_id="confirm_offline_1")

    def get_task_run_metadata(self, task_run_id: str) -> dict:
        return dict(self.metadata)

    def get_task_run(self, task_run_id: str):
        if task_run_id in self.task_runs:
            return self.task_runs[task_run_id]
        return SimpleNamespace(
            task_run_id=task_run_id,
            session_id="session_1",
            requirement_id="req_1",
            trigger_message_id="msg_1",
            source_ref="chat_1",
            source_type="p2p",
            title="Offline Document Import",
            metadata_json="{}",
            session_documents=[
                {
                    "document_id": "doc_current",
                    "url": "https://feishu.example/doc_current",
                    "title": "Current Document",
                    "is_current": True,
                }
            ],
        )

    def list_task_runs(self, *, requirement_id: str | None = None, session_id: str | None = None, limit: int = 20):
        items = list(self.task_run_summaries)
        if requirement_id:
            items = [item for item in items if str(getattr(item, "requirement_id", "") or "") == requirement_id]
        if session_id:
            items = [item for item in items if str(getattr(item, "session_id", "") or "") == session_id]
        return items[:limit]

    def get_artifact_task_run_id(self, artifact_id: str) -> str | None:
        return self.artifact_sources.get(artifact_id)

    def bind_requirement(self, task_run_id: str, requirement_id: str) -> None:
        detail = self.task_runs.get(task_run_id)
        if detail is not None:
            detail.requirement_id = requirement_id


class _ReplySender:
    def deliver_reply(self, message, mode, reply_preview, **kwargs):
        return {
            "session_id": message.session_id,
            "mode": mode,
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "artifacts": kwargs.get("artifacts", []) or [],
        }


class _ResponseFormatter:
    def format_clarification_reply(self, *, intent: str, clarification: dict) -> str:
        return f"{clarification['question']}|{clarification['reason']}"


class _MessageResourceAPI:
    def download_message_resource(self, *, message_id: str, file_key: str, resource_type: str = "file"):
        return "Background\n\nFinish the requirement board.".encode("utf-8"), "text/plain"


class _DocExecution:
    def __init__(self) -> None:
        self.sync_calls: list[dict] = []

    def sync_package_to_session_doc(self, package: dict, **kwargs):
        self.sync_calls.append({"package": package, **kwargs})
        return SimpleNamespace(
            status="ready",
            summary_lines=["- Updated document: https://feishu.example/doc_current"],
            mode="updated",
            document_info={"document_id": "doc_current", "url": "https://feishu.example/doc_current", "version": 2},
        )

    def build_document_artifact(
        self,
        *,
        session_id: str,
        package: dict,
        sync_result,
        fallback_provider: str,
        task_run_id: str | None = None,
    ):
        return {
            "artifact_type": "document",
            "provider": fallback_provider,
            "status": "ready",
            "title": package.get("title") or "Offline Document",
            "url": "https://feishu.example/doc_current",
            "preview": package,
            "version": 2,
        }

    def format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        return f"Applied {package.get('title')}"


class _OfflineExecution:
    def parsed_package_local_artifact(self, **kwargs):
        return {
            "artifact_type": "document",
            "provider": "offline_upload",
            "status": "ready",
            "title": "Offline Reference",
            "url": "/api/artifacts/doc/offline.md",
            "preview": kwargs["package"],
            "version": 1,
        }


class _ResultPersistence:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def persist_task_run_result(self, task_run_id: str, **kwargs) -> None:
        self.calls.append({"task_run_id": task_run_id, **kwargs})


class _RequirementService:
    def __init__(self, *, with_follow_up_artifacts: bool = False) -> None:
        self.with_follow_up_artifacts = with_follow_up_artifacts
        self.bound: list[dict] = []
        self.created: list[dict] = []

    def get_requirement(self, requirement_id: str):
        payload = {
            "requirement_id": requirement_id,
            "current_document": {
                "document_id": "doc_current",
                "url": "https://feishu.example/doc_current",
                "title": "Current Document",
                "version": 2,
                "section_snapshot": [
                    {
                        "heading": "离线导入内容",
                        "paragraphs": ["Background"],
                    }
                ],
            },
        }
        if self.with_follow_up_artifacts:
            payload["current_slides"] = SimpleNamespace(artifact_id="artifact_slides_1")
            payload["current_canvas"] = SimpleNamespace(artifact_id="artifact_canvas_1")
        else:
            payload["current_slides"] = None
            payload["current_canvas"] = None
        return SimpleNamespace(**payload)

    def bind_task_run(
        self,
        *,
        task_run_id: str,
        requirement_id: str,
        session_id: str | None = None,
        message_id: str | None = None,
        source_type: str | None = None,
    ):
        self.bound.append(
            {
                "task_run_id": task_run_id,
                "requirement_id": requirement_id,
                "session_id": session_id,
                "message_id": message_id,
                "source_type": source_type,
            }
        )
        task_run_service = getattr(self, "task_run_service", None)
        if task_run_service is not None:
            task_run_service.bind_requirement(task_run_id, requirement_id)
        return SimpleNamespace(task_run_id=task_run_id, requirement_id=requirement_id)

    def create_requirement(
        self,
        *,
        title: str,
        primary_session_id: str,
        summary: str | None = None,
        created_by: str | None = None,
        source_message_id: str | None = None,
        source_type: str | None = None,
        metadata: dict | None = None,
    ):
        requirement_id = f"req_created_{len(self.created) + 1}"
        self.created.append(
            {
                "requirement_id": requirement_id,
                "title": title,
                "primary_session_id": primary_session_id,
                "summary": summary,
                "created_by": created_by,
                "source_message_id": source_message_id,
                "source_type": source_type,
                "metadata": metadata or {},
            }
        )
        return SimpleNamespace(requirement_id=requirement_id)


class OfflineDocumentFlowTests(unittest.TestCase):
    def test_prepare_offline_document_submission_creates_confirmation(self) -> None:
        workflow = SimpleNamespace(
            task_run_service=_TaskRunService(),
            message_resource_api=_MessageResourceAPI(),
            offline_document_parser=OfflineDocumentParser(),
            offline_document_merge_service=OfflineDocumentMergeService(),
            response_formatter=_ResponseFormatter(),
            reply_sender=_ReplySender(),
            requirement_service=_RequirementService(with_follow_up_artifacts=True),
        )
        execution = WorkflowOfflineDocumentExecution(workflow)
        message = FeishuMessageContext(
            session_id="session_1",
            sender_id="user_1",
            message_id="msg_1",
            chat_id="chat_1",
            chat_type="p2p",
            message_type="file",
            text="meeting.txt",
            raw_text="meeting.txt",
            file_key="file_1",
            file_name="meeting.txt",
            is_mentioned=False,
        )

        result = execution.prepare_offline_document_submission(message, task_run_id="run_1")

        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["task_run_status"], "waiting_confirmation")
        self.assertIn("请选择处理方式", workflow.task_run_service.confirmation["prompt"])
        self.assertEqual(
            workflow.task_run_service.confirmation["options"],
            [
                "仅更新当前文档",
                "更新文档 + 当前 PPT",
                "更新文档 + 当前 Canvas",
                "更新文档 + 当前 PPT + 当前 Canvas",
                "仅作为参考材料暂存",
            ],
        )
        self.assertIn("offline_document_confirmation", workflow.task_run_service.metadata)
        self.assertEqual(
            workflow.task_run_service.metadata["offline_document_confirmation"]["available_follow_up_targets"],
            ["canvas", "slides"],
        )
        merge_summary = workflow.task_run_service.metadata["offline_document_confirmation"]["merge_summary"]
        self.assertEqual(merge_summary["updated_headings"], ["离线导入内容"])
        self.assertEqual(merge_summary["new_headings"], [])
        self.assertIn("联动当前 PPT / Canvas", result["reply_preview"])
        self.assertIn("拟更新章节 1 个", result["reply_preview"])

    def test_prepare_offline_document_submission_ignores_duplicate_upload(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.task_run_summaries = [
            SimpleNamespace(task_run_id="run_1", requirement_id="req_1", session_id="session_1"),
            SimpleNamespace(task_run_id="run_existing", requirement_id="req_1", session_id="session_1"),
        ]
        existing_bytes = "Background\n\nFinish the requirement board.".encode("utf-8")
        existing_hash = hashlib.sha256(existing_bytes).hexdigest()
        task_run_service.task_runs["run_existing"] = SimpleNamespace(
            task_run_id="run_existing",
            session_id="session_1",
            requirement_id="req_1",
            title="Previous Offline Upload",
            status="completed",
            stage="offline_document_duplicate",
            metadata_json=json.dumps(
                {
                    "offline_document_last_record": {
                        "submission_id": "run_existing",
                        "task_run_id": "run_existing",
                        "file_name": "meeting.txt",
                        "file_extension": ".txt",
                        "file_sha256": existing_hash,
                        "status": "merged",
                        "merge_summary": {"summary_lines": ["拟更新章节 1 个"]},
                        "merge_plan": {"summary_lines": ["拟更新章节 1 个"]},
                    }
                },
                ensure_ascii=False,
            ),
            session_documents=[],
        )
        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            message_resource_api=_MessageResourceAPI(),
            offline_document_parser=OfflineDocumentParser(),
            offline_document_merge_service=OfflineDocumentMergeService(),
            response_formatter=_ResponseFormatter(),
            reply_sender=_ReplySender(),
            requirement_service=_RequirementService(with_follow_up_artifacts=True),
        )
        execution = WorkflowOfflineDocumentExecution(workflow)
        message = FeishuMessageContext(
            session_id="session_1",
            sender_id="user_1",
            message_id="msg_1",
            chat_id="chat_1",
            chat_type="p2p",
            message_type="file",
            text="meeting.txt",
            raw_text="meeting.txt",
            file_key="file_1",
            file_name="meeting.txt",
            is_mentioned=False,
        )

        result = execution.prepare_offline_document_submission(message, task_run_id="run_1")

        self.assertEqual(result["task_run_status"], "completed")
        self.assertEqual(result["task_run_stage"], "offline_document_duplicate")
        self.assertIn("本次上传已忽略", result["reply_preview"])
        self.assertIn("run_existing", result["reply_preview"])
        self.assertNotIn("offline_document_confirmation", task_run_service.metadata)
        last_record = task_run_service.metadata["offline_document_last_record"]
        self.assertEqual(last_record["status"], "ignored_duplicate")
        self.assertEqual(last_record["duplicate_of_submission_id"], "run_existing")
        duplicate_steps = [step for step in task_run_service.steps if step["step_key"] == "offline_document_duplicate"]
        self.assertEqual(duplicate_steps[-1]["status"], "done")

    def test_resume_after_requirement_confirmation_continues_offline_document_flow(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.metadata = {
            "requirement_confirmation": {
                "confirmation_id": "confirm_requirement_1",
                "instruction": "评审纪要.docx",
                "raw_text": "评审纪要.docx",
                "message_type": "file",
                "file_key": "file_docx_1",
                "file_name": "评审纪要.docx",
                "chat_id": "chat_1",
                "chat_type": "p2p",
                "is_mentioned": False,
                "candidates": [
                    {
                        "requirement_id": "req_target_1",
                        "title": "校园活动报名系统",
                    }
                ],
                "new_option": "新建一个需求",
            }
        }
        task_run_service.task_runs["run_1"] = SimpleNamespace(
            task_run_id="run_1",
            session_id="session_1",
            requirement_id="",
            trigger_message_id="msg_file_1",
            source_ref="chat_1",
            source_type="p2p",
            title="Offline Document Import",
            metadata_json="{}",
            session_documents=[],
        )
        requirement_service = _RequirementService()
        requirement_service.task_run_service = task_run_service
        offline_calls: list[dict] = []

        class _RequirementConfirmOfflineExecution:
            def prepare_offline_document_submission(self, message, *, task_run_id: str):
                offline_calls.append(
                    {
                        "task_run_id": task_run_id,
                        "message_type": message.message_type,
                        "file_key": message.file_key,
                        "file_name": message.file_name,
                        "text": message.text,
                        "is_mentioned": message.is_mentioned,
                    }
                )
                return {
                    "session_id": message.session_id,
                    "mode": "offline_document",
                    "reply_preview": "已继续处理离线文档",
                    "reply_sent": False,
                    "reply_error": None,
                    "artifacts": [],
                    "task_run_id": task_run_id,
                }

        result_persistence = _ResultPersistence()
        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            requirement_service=requirement_service,
            offline_document_execution=_RequirementConfirmOfflineExecution(),
            result_persistence=result_persistence,
        )

        result = WorkbenchRevisionWorkflow(workflow).resume_task_run_after_confirmation(
            "run_1",
            confirmation_id="confirm_requirement_1",
            answer_value="1. 校园活动报名系统",
            answered_by="pilot_admin_web",
        )

        self.assertEqual(result["mode"], "offline_document")
        self.assertEqual(len(offline_calls), 1)
        self.assertEqual(offline_calls[0]["task_run_id"], "run_1")
        self.assertEqual(offline_calls[0]["message_type"], "file")
        self.assertEqual(offline_calls[0]["file_key"], "file_docx_1")
        self.assertEqual(offline_calls[0]["file_name"], "评审纪要.docx")
        self.assertFalse(offline_calls[0]["is_mentioned"])
        self.assertEqual(requirement_service.bound[0]["requirement_id"], "req_target_1")
        self.assertEqual(task_run_service.task_runs["run_1"].requirement_id, "req_target_1")
        self.assertEqual(result_persistence.calls[0]["task_run_id"], "run_1")

    def test_resume_after_offline_document_confirmation_applies_to_current_doc_and_triggers_selected_follow_ups(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.metadata = {
            "offline_document_confirmation": {
                "confirmation_id": "confirm_offline_1",
                "instruction": "Please merge the offline update",
                "file_name": "meeting.txt",
                "available_follow_up_targets": ["slides", "canvas"],
                "package": {
                    "title": "meeting",
                    "sections": [{"heading": "Offline Update", "paragraphs": ["Finish the requirement board."]}],
                },
            }
        }
        result_persistence = _ResultPersistence()
        doc_execution = _DocExecution()
        slides_calls: list[dict] = []
        canvas_calls: list[dict] = []

        def revise_slides_from_task_run(source_task_run_id: str, **kwargs):
            slides_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_slides_follow_up")

        def revise_canvas_from_task_run(source_task_run_id: str, **kwargs):
            canvas_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_canvas_follow_up")

        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            requirement_service=_RequirementService(with_follow_up_artifacts=True),
            doc_execution=doc_execution,
            offline_document_execution=_OfflineExecution(),
            result_persistence=result_persistence,
            revise_slides_from_task_run=revise_slides_from_task_run,
            revise_canvas_from_task_run=revise_canvas_from_task_run,
        )

        result = WorkbenchRevisionWorkflow(workflow).resume_task_run_after_confirmation(
            "run_1",
            confirmation_id="confirm_offline_1",
            answer_value="更新文档 + 当前 PPT",
            answered_by="pilot_admin_web",
            override_instruction="只同步第 3 页，图片保留原尺寸",
        )

        self.assertEqual(result["mode"], "doc")
        self.assertEqual(doc_execution.sync_calls[0]["target_document"]["document_id"], "doc_current")
        self.assertIn("[用户补充约束]", doc_execution.sync_calls[0]["instruction"])
        self.assertIn("只同步第 3 页", doc_execution.sync_calls[0]["instruction"])
        self.assertEqual(result_persistence.calls[0]["task_run_id"], "run_1")
        self.assertEqual(slides_calls[0]["source_task_run_id"], "run_slides_source")
        self.assertEqual(slides_calls[0]["requested_by"], "offline_document")
        self.assertEqual(slides_calls[0]["artifact_id"], "artifact_slides_1")
        self.assertIn("追加 1 页", slides_calls[0]["instruction"])
        self.assertIn("只同步第 3 页", slides_calls[0]["instruction"])
        self.assertEqual(len(canvas_calls), 0)
        self.assertIn("已应用补充约束", result["reply_preview"])
        self.assertIn("已触发当前 PPT 跟随更新", result["reply_preview"])
        self.assertNotIn("已触发当前 Canvas 跟随更新", result["reply_preview"])
        follow_up_steps = [step for step in task_run_service.steps if step["step_key"] == "offline_document_follow_up"]
        self.assertEqual(follow_up_steps[-1]["status"], "done")
        self.assertEqual(follow_up_steps[-1]["output_payload"]["canvas"]["reason"], "not_selected")

    def test_resume_after_offline_document_confirmation_can_apply_doc_only(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.metadata = {
            "offline_document_confirmation": {
                "confirmation_id": "confirm_offline_1",
                "instruction": "Please merge the offline update",
                "file_name": "meeting.txt",
                "available_follow_up_targets": ["slides", "canvas"],
                "package": {
                    "title": "meeting",
                    "sections": [{"heading": "Offline Update", "paragraphs": ["Finish the requirement board."]}],
                },
            }
        }
        result_persistence = _ResultPersistence()
        doc_execution = _DocExecution()
        slides_calls: list[dict] = []
        canvas_calls: list[dict] = []

        def revise_slides_from_task_run(source_task_run_id: str, **kwargs):
            slides_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_slides_follow_up")

        def revise_canvas_from_task_run(source_task_run_id: str, **kwargs):
            canvas_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_canvas_follow_up")

        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            requirement_service=_RequirementService(with_follow_up_artifacts=True),
            doc_execution=doc_execution,
            offline_document_execution=_OfflineExecution(),
            result_persistence=result_persistence,
            revise_slides_from_task_run=revise_slides_from_task_run,
            revise_canvas_from_task_run=revise_canvas_from_task_run,
        )

        result = WorkbenchRevisionWorkflow(workflow).resume_task_run_after_confirmation(
            "run_1",
            confirmation_id="confirm_offline_1",
            answer_value="仅更新当前文档",
            answered_by="pilot_admin_web",
        )

        self.assertEqual(result["mode"], "doc")
        self.assertEqual(len(slides_calls), 0)
        self.assertEqual(len(canvas_calls), 0)
        self.assertIn("仅更新当前文档", result["reply_preview"])
        follow_up_steps = [step for step in task_run_service.steps if step["step_key"] == "offline_document_follow_up"]
        self.assertEqual(follow_up_steps[-1]["status"], "skipped")

    def test_resume_after_legacy_offline_confirmation_keeps_follow_up_targets(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.metadata = {
            "offline_document_confirmation": {
                "confirmation_id": "confirm_offline_1",
                "instruction": "Please merge the offline update",
                "file_name": "meeting.txt",
                "available_follow_up_targets": ["slides", "canvas"],
                "package": {
                    "title": "meeting",
                    "sections": [{"heading": "Offline Update", "paragraphs": ["Finish the requirement board."]}],
                },
            }
        }
        result_persistence = _ResultPersistence()
        doc_execution = _DocExecution()
        slides_calls: list[dict] = []
        canvas_calls: list[dict] = []

        def revise_slides_from_task_run(source_task_run_id: str, **kwargs):
            slides_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_slides_follow_up")

        def revise_canvas_from_task_run(source_task_run_id: str, **kwargs):
            canvas_calls.append({"source_task_run_id": source_task_run_id, **kwargs})
            return SimpleNamespace(task_run_id="run_canvas_follow_up")

        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            requirement_service=_RequirementService(with_follow_up_artifacts=True),
            doc_execution=doc_execution,
            offline_document_execution=_OfflineExecution(),
            result_persistence=result_persistence,
            revise_slides_from_task_run=revise_slides_from_task_run,
            revise_canvas_from_task_run=revise_canvas_from_task_run,
        )

        result = WorkbenchRevisionWorkflow(workflow).resume_task_run_after_confirmation(
            "run_1",
            confirmation_id="confirm_offline_1",
            answer_value="应用到当前文档",
            answered_by="pilot_admin_web",
        )

        self.assertEqual(result["mode"], "doc")
        self.assertEqual(len(slides_calls), 1)
        self.assertEqual(len(canvas_calls), 1)
        self.assertIn("已触发当前 PPT 跟随更新", result["reply_preview"])
        self.assertIn("已触发当前 Canvas 跟随更新", result["reply_preview"])

    def test_resume_after_offline_document_confirmation_can_archive_only(self) -> None:
        task_run_service = _TaskRunService()
        task_run_service.metadata = {
            "offline_document_confirmation": {
                "confirmation_id": "confirm_offline_1",
                "instruction": "Please store this only",
                "file_name": "meeting.txt",
                "available_follow_up_targets": ["slides", "canvas"],
                "package": {
                    "title": "meeting",
                    "sections": [{"heading": "Offline Update", "paragraphs": ["Finish the requirement board."]}],
                },
            }
        }
        result_persistence = _ResultPersistence()
        workflow = SimpleNamespace(
            task_run_service=task_run_service,
            requirement_service=_RequirementService(),
            doc_execution=_DocExecution(),
            offline_document_execution=_OfflineExecution(),
            result_persistence=result_persistence,
        )

        result = WorkbenchRevisionWorkflow(workflow).resume_task_run_after_confirmation(
            "run_1",
            confirmation_id="confirm_offline_1",
            answer_value="仅作为参考材料暂存",
            answered_by="pilot_admin_web",
        )

        self.assertIn("参考材料暂存", result["reply_preview"])
        self.assertEqual(result_persistence.calls[0]["result"]["artifacts"][0]["provider"], "offline_upload")


if __name__ == "__main__":
    unittest.main()
