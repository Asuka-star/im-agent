import json
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.task import TaskItem
from app.schemas.feishu_event import FeishuMessageContext
from app.services.feishu_workflow import FeishuWorkflowService
from app.services.graph.nodes.context_loader import context_loader_node
from app.services.graph.state import WorkerResult, WorkspaceCommand
from app.services.graph.runner import GraphRunner
from app.services.request_router import RequestRouter, RouteDecision


class FakeLLMService:
    def __init__(self, result: dict | None = None, *, configured: bool = True) -> None:
        self.result = result or {}
        self.configured = configured
        self.calls: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return self.configured

    def interpret_workspace_command(self, workspace_context: str, instruction: str) -> dict:
        self.calls.append((workspace_context, instruction))
        return dict(self.result)

    def resolve_analysis_request(self, workspace_context: str, instruction: str, route: str) -> dict:
        self.calls.append((workspace_context, instruction))
        return {
            "reason": f"{route} analysis",
            "summary": f"{route} summary",
            "risks": [f"{route} risk"] if route == "risks" else [],
            "next_actions": [f"{route} next action"],
        }


class FakeTaskRunService:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.metadata: list[tuple[str, dict]] = []
        self.metadata_by_run: dict[str, dict] = {}
        self.confirmation_index = 0
        self.updates: list[dict] = []

    def upsert_step(self, task_run_id: str, **kwargs):
        self.steps.append({"task_run_id": task_run_id, **kwargs})

    def merge_task_run_metadata(self, task_run_id: str, patch: dict):
        current = dict(self.metadata_by_run.get(task_run_id, {}))
        current.update(patch)
        self.metadata_by_run[task_run_id] = current
        self.metadata.append((task_run_id, patch))

    def get_task_run_metadata(self, task_run_id: str) -> dict:
        return dict(self.metadata_by_run.get(task_run_id, {}))

    def update_task_run(self, task_run_id: str, **kwargs):
        if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
            self.metadata_by_run[task_run_id] = dict(kwargs["metadata"])
        self.updates.append({"task_run_id": task_run_id, **kwargs})

    def create_confirmation(self, task_run_id: str, *, prompt: str, options: list[str] | None = None):
        self.confirmation_index += 1
        return SimpleNamespace(
            confirmation_id=f"confirm_{self.confirmation_index}",
            task_run_id=task_run_id,
            prompt=prompt,
            options=options or [],
        )


class FakeWorkflow:
    def __init__(self, llm_service: FakeLLMService) -> None:
        self.request_router = RequestRouter()
        self.llm_service = llm_service
        self.task_run_service = FakeTaskRunService()


class FakeStatusExecution:
    def prepare_status_execution(self, message, *, llm_result, active_episode_id=None, task_run_id=None):
        return {
            "reply_preview": "当前有 1 项任务",
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }


class FakeArtifactExecution:
    def __init__(self, worker: str, delay: float = 0.0) -> None:
        self.worker = worker
        self.delay = delay
        self.calls: list[dict] = []

    def prepare_doc_execution(self, message, **kwargs):
        return self._result(message, kwargs)

    def prepare_slides_execution(self, message, **kwargs):
        return self._result(message, kwargs)

    def prepare_canvas_execution(self, message, **kwargs):
        return self._result(message, kwargs)

    def _result(self, message, kwargs):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append({"message": message, "kwargs": kwargs})
        return {
            "reply_preview": f"{self.worker} ready",
            "analysis": None,
            "artifacts": [
                {
                    "artifact_type": self.worker,
                    "provider": "fake",
                    "title": self.worker,
                    "url": f"/{self.worker}",
                    "preview": {"worker": self.worker},
                }
            ],
            "close_title": self.worker,
        }


class FakeReplySender:
    def __init__(self) -> None:
        self.delivered: list[dict] = []

    def deliver_reply(self, message, mode, reply_preview, **kwargs):
        result = {
            "session_id": message.session_id,
            "mode": mode,
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "analysis": kwargs.get("analysis"),
            "artifacts": kwargs.get("artifacts", []),
        }
        self.delivered.append(result)
        return result


class FakeMemoryService:
    def __init__(self) -> None:
        self.saved_rounds: list[dict] = []

    def save_round(self, **kwargs):
        self.saved_rounds.append(kwargs)

    def build_discussion_block(self, *args, **kwargs):
        return "discussion context"


class FakeResultPersistence:
    def __init__(self) -> None:
        self.persisted: list[dict] = []

    def persist_task_run_result(self, task_run_id: str, **kwargs):
        self.persisted.append({"task_run_id": task_run_id, **kwargs})


class FakeDeliveryArtifactService:
    def __init__(self) -> None:
        self.persisted: list[dict] = []

    def persist_bundle(self, manifest: dict, *, task_run_id: str, session_id: str) -> dict:
        self.persisted.append({"manifest": manifest, "task_run_id": task_run_id, "session_id": session_id})
        return {
            "artifact_type": "delivery_bundle",
            "provider": "fake",
            "title": manifest.get("title") or "delivery",
            "url": "/delivery",
            "preview": manifest,
            "version": 1,
        }


class FakePresentationTool:
    def plan_revision(self, package, instruction, llm_result=None):
        return SimpleNamespace(mutation_required=True, operations=[{"type": "update"}])

    def revise_deterministic(self, package, instruction, *, edit_plan):
        revised = json.loads(json.dumps(package))
        first_slide = revised["slides"][0]
        first_slide["speaker_notes"] = f"{first_slide.get('speaker_notes') or ''}\n修订要求：{instruction}".strip()
        revised["artifact_edit_plan"] = {"operations": edit_plan.operations}
        return revised

    def package_changed(self, before, after):
        return before != after

    def persist_artifact(self, package, *, provider, session_id, task_run_id):
        return {
            "artifact_type": "slides_package",
            "provider": provider,
            "title": package.get("theme") or "slides",
            "url": "/slides-revised",
            "preview": package,
            "version": package.get("version") or 1,
        }

    def format_reply(self, package, *, artifact=None):
        return "slides revised"


class FakeCanvasTool:
    def plan_revision(self, scene, instruction, llm_result=None):
        return SimpleNamespace(mutation_required=True, operations=[{"type": "update"}])

    def revise_scene_deterministic(self, scene, instruction, *, edit_plan):
        revised = json.loads(json.dumps(scene))
        first_shape = revised["shapes"][0]
        first_shape["text"] = f"{first_shape.get('text') or ''}\n修订：{instruction}".strip()
        revised["artifact_edit_plan"] = {"operations": edit_plan.operations}
        return revised

    def generate_flow_artifact(self, *, title, instruction, llm_result, workspace_context, task_run_id, session_id):
        scene = dict(llm_result.get("canvas") or {})
        scene["title"] = title
        return {
            "artifact_type": "canvas",
            "provider": "fake",
            "title": title,
            "url": "/canvas-revised",
            "preview": scene,
            "version": scene.get("version") or 1,
        }

    def format_reply(self, artifact):
        return "canvas revised"


class FakeResponseFormatter:
    def format_task_status_update_reply(self, task, status):
        return f"{task.title} -> {status}"

    def format_analysis_reply(self, analysis, mode):
        return analysis.summary

    def format_help_reply(self, reason=None):
        return f"help ready: {reason or ''}".strip()


class FakeAnalysisExecution:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def prepare_analysis_execution(self, message, *, llm_result, workspace_context, active_episode_id, intent):
        self.calls.append(
            {
                "message": message,
                "llm_result": llm_result,
                "workspace_context": workspace_context,
                "active_episode_id": active_episode_id,
                "intent": intent,
            }
        )
        return {
            "reply_preview": f"{intent} ready",
            "analysis": None,
            "artifacts": [],
            "close_title": intent,
        }


class ExecutableFakeWorkflow(FakeWorkflow):
    def __init__(self, llm_service: FakeLLMService) -> None:
        super().__init__(llm_service)
        self.status_execution = FakeStatusExecution()
        self.doc_execution = FakeArtifactExecution("doc")
        self.slides_execution = FakeArtifactExecution("slides")
        self.canvas_execution = FakeArtifactExecution("canvas")
        self.analysis_execution = FakeAnalysisExecution()
        self.reply_sender = FakeReplySender()
        self.memory_service = FakeMemoryService()
        self.result_persistence = FakeResultPersistence()
        self.delivery_artifact_service = FakeDeliveryArtifactService()
        self.response_formatter = FakeResponseFormatter()
        self.context_artifacts: list[dict] = []

    def _context_tasks_for_message(self, message):
        return [
            TaskItem(
                title="制作ppt",
                owner="张三",
                priority="medium",
                due_date="TBD",
                status="draft",
                notes="",
            )
        ]

    def _sender_actor_names_for_message(self, message):
        return ["张三"]

    def _presentation_tool(self):
        return FakePresentationTool()

    def _canvas_tool(self):
        return FakeCanvasTool()

    def graph_context_artifacts_loader(self, session_id, task_run_id):
        return list(self.context_artifacts)

    def _pause_for_clarification(
        self,
        message,
        *,
        intent,
        clarification,
        active_episode_id,
        task_run_id,
        workspace_context=None,
        artifacts=None,
    ):
        confirmation = self.task_run_service.create_confirmation(
            task_run_id,
            prompt=clarification["question"],
            options=clarification.get("options", []),
        )
        self.task_run_service.merge_task_run_metadata(
            task_run_id,
            {
                "resume_after_confirmation": {
                    "confirmation_id": confirmation.confirmation_id,
                    "intent": intent,
                    "instruction": message.text,
                    "workspace_context": workspace_context or "",
                    "active_episode_id": active_episode_id,
                    "question": clarification.get("question"),
                    "reason": clarification.get("reason"),
                    "options": clarification.get("options", []),
                }
            },
        )
        return {
            "session_id": message.session_id,
            "mode": intent,
            "reply_preview": clarification["question"],
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
            "artifacts": artifacts or [],
            "pending_confirmation": True,
            "confirmation_id": confirmation.confirmation_id,
            "task_run_status": "waiting_confirmation",
        }


class SlowSessionDocumentService:
    def get_current_document(self, session_id: str):
        time.sleep(0.2)
        return {"session_id": session_id, "document_id": "doc_1", "title": "Current doc"}


class SlowMemoryService:
    def get_episode_messages(self, session_id: str, *, episode_id: int, exclude_message_id=None, limit=10):
        time.sleep(0.2)
        return [SimpleNamespace(sender_id="ou_2", content="recent context")]


class ParallelContextWorkflow(FakeWorkflow):
    def __init__(self) -> None:
        super().__init__(FakeLLMService(configured=False))
        self.session_document_service = SlowSessionDocumentService()
        self.memory_service = SlowMemoryService()

    def _context_tasks_for_message(self, message):
        time.sleep(0.2)
        return [
            TaskItem(
                title="Parallel task",
                owner="张三",
                priority="medium",
                due_date="TBD",
                status="draft",
                notes="",
            )
        ]

    def graph_context_artifacts_loader(self, session_id: str, task_run_id: str | None):
        time.sleep(0.2)
        return [{"artifact_type": "document", "title": "Existing doc"}]


class TimeoutContextWorkflow(FakeWorkflow):
    def __init__(self) -> None:
        super().__init__(FakeLLMService(configured=False))
        self.session_document_service = SimpleNamespace(
            get_current_document=lambda session_id: {"session_id": session_id, "document_id": "doc_fast"}
        )

    def _context_tasks_for_message(self, message):
        time.sleep(0.3)
        return []


def message(text: str) -> FeishuMessageContext:
    return FeishuMessageContext(
        session_id="chat_1",
        sender_id="ou_1",
        message_id="om_1",
        chat_id="oc_1",
        chat_type="group",
        message_type="text",
        text=text,
        raw_text=text,
        is_mentioned=True,
    )


class GraphRunnerTests(unittest.TestCase):
    def test_shadow_interprets_delete_task_as_task_remove(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "remove",
                    "object": "task",
                    "target_text": "制作ppt",
                    "destructive": True,
                    "confidence": 0.94,
                    "reason": "用户要求删除已有任务",
                }
            )
        )

        state = GraphRunner(workflow).run_shadow(message("删除制作ppt的任务"), workspace_context="当前任务：制作ppt")

        self.assertEqual(state.command.operation, "remove")
        self.assertEqual(state.command.object, "task")
        self.assertEqual(state.command.target_text, "制作ppt")
        self.assertTrue(state.command.destructive)
        self.assertEqual(state.plan.steps[0].worker, "task")
        self.assertEqual(state.plan.steps[0].operation, "remove")

    def test_compound_outputs_are_planned_as_parallel_workers(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["doc", "slides", "canvas"],
                    "artifact_goals": {
                        "doc": "整理成项目说明文档",
                        "slides": "生成汇报 PPT 大纲",
                        "canvas": "画出协作流程图",
                    },
                    "confidence": 0.9,
                }
            )
        )

        state = GraphRunner(workflow).run_shadow(message("整理成文档、PPT 和流程图"))

        self.assertEqual(state.command.requested_outputs, ["doc", "slides", "canvas"])
        self.assertEqual([step.worker for step in state.plan.steps], ["doc", "slides", "canvas"])
        self.assertTrue(all(step.can_run_parallel for step in state.plan.steps))
        self.assertEqual(state.plan.steps[0].input["agent"], "DocAgent")
        self.assertEqual(state.plan.steps[0].input["goal"], "整理成项目说明文档")
        self.assertEqual(state.plan.steps[1].input["requested_outputs"], ["slides"])

    def test_interpreter_preserves_legacy_compound_outputs(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "slides",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["slides"],
                    "confidence": 0.9,
                }
            )
        )

        state = GraphRunner(workflow).run_shadow(
            message("整理成文档、PPT 和流程图"),
            legacy_route={
                "route": "slides",
                "source": "llm",
                "confidence": 0.9,
                "requested_outputs": ["doc", "slides", "canvas"],
            },
        )

        self.assertEqual(state.command.requested_outputs, ["doc", "slides", "canvas"])
        self.assertEqual([step.worker for step in state.plan.steps], ["doc", "slides", "canvas"])

    def test_interpreter_promotes_legacy_compound_outputs_when_llm_returns_summary(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "summary",
                    "operation": "analyze",
                    "object": "summary",
                    "confidence": 0.95,
                }
            )
        )

        state = GraphRunner(workflow).run_shadow(
            message("总结项目进展，同时生成文档、PPT 和流程图"),
            legacy_route={
                "route": "doc",
                "source": "llm",
                "confidence": 0.9,
                "requested_outputs": ["doc", "slides", "canvas"],
            },
        )

        self.assertEqual(state.command.operation, "generate")
        self.assertEqual(state.command.object, "workspace")
        self.assertEqual(state.command.route, "doc")
        self.assertEqual(state.command.requested_outputs, ["doc", "slides", "canvas"])
        self.assertEqual([step.worker for step in state.plan.steps], ["doc", "slides", "canvas"])

    def test_unconfigured_llm_produces_unknown_command_without_raising(self) -> None:
        workflow = FakeWorkflow(FakeLLMService(configured=False))

        state = GraphRunner(workflow).run_shadow(message("帮我看一下这个怎么处理"))

        self.assertEqual(state.command.mode, "clarify")
        self.assertEqual(state.command.operation, "clarify")
        self.assertTrue(state.command.needs_clarification)
        self.assertEqual(workflow.llm_service.calls, [])

    def test_context_loader_fetches_independent_sources_in_parallel(self) -> None:
        workflow = ParallelContextWorkflow()
        state = {
            "message": message("load context").model_dump(mode="json"),
            "task_run_id": "run_1",
            "active_episode_id": 7,
            "context": {"session_id": "chat_1"},
            "trace": [],
        }

        with patch("app.services.graph.nodes.context_loader.settings.langgraph_max_parallel_workers", 3):
            started = time.perf_counter()
            result = context_loader_node(workflow)(state)
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.65)
        context = result["context"]
        self.assertEqual(len(context["tasks"]), 1)
        self.assertEqual(context["current_document"]["document_id"], "doc_1")
        self.assertEqual(len(context["recent_messages"]), 1)
        self.assertEqual(len(context["artifacts"]), 1)
        source_traces = result["trace"][-1]["sources"]
        self.assertEqual(
            {item["field"] for item in source_traces},
            {"artifacts", "current_document", "recent_messages", "tasks"},
        )
        self.assertTrue(all(item["status"] == "done" for item in source_traces))
        self.assertEqual(context["source_docs"][0]["document_id"], "doc_1")
        self.assertEqual(context["loaded_sources"], source_traces)

    def test_context_loader_records_output_requirements_from_command(self) -> None:
        workflow = ParallelContextWorkflow()
        state = {
            "message": message("make document and slides").model_dump(mode="json"),
            "task_run_id": "run_1",
            "active_episode_id": 7,
            "command": WorkspaceCommand(
                operation="generate",
                object="workspace",
                requested_outputs=["doc", "slides"],
                artifact_goals={"doc": "整理说明文档"},
            ).model_dump(mode="json"),
            "context": {"session_id": "chat_1"},
            "trace": [],
        }

        result = context_loader_node(workflow)(state)
        context = result["context"]

        self.assertEqual(context["output_requirements"]["requested_outputs"], ["doc", "slides"])
        self.assertEqual(context["output_requirements"]["artifact_goals"]["doc"], "整理说明文档")
        self.assertEqual(context["missing_fields"], [])

    def test_context_loader_marks_missing_doc_target_for_revision(self) -> None:
        workflow = ExecutableFakeWorkflow(FakeLLMService(configured=False))
        state = {
            "message": message("更新这份文档").model_dump(mode="json"),
            "task_run_id": "run_1",
            "command": WorkspaceCommand(
                route="doc",
                operation="update",
                object="doc",
                confidence=0.95,
            ).model_dump(mode="json"),
            "context": {"session_id": "chat_1"},
            "trace": [],
        }

        result = context_loader_node(workflow)(state)
        context = result["context"]

        self.assertTrue(any(item["field"] == "current_document" for item in context["missing_fields"]))

    def test_context_loader_times_out_slow_sources_without_blocking_graph(self) -> None:
        workflow = TimeoutContextWorkflow()
        state = {
            "message": message("load context").model_dump(mode="json"),
            "task_run_id": "run_1",
            "context": {"session_id": "chat_1"},
            "trace": [],
        }

        with patch("app.services.graph.nodes.context_loader.settings.langgraph_node_timeout_seconds", 0.05):
            started = time.perf_counter()
            result = context_loader_node(workflow)(state)
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.2)
        context = result["context"]
        self.assertEqual(context["current_document"]["document_id"], "doc_fast")
        self.assertEqual(context["tasks"], [])
        self.assertTrue(any(item.get("field") == "tasks" for item in context["errors"]))
        task_trace = next(item for item in result["trace"][-1]["sources"] if item["field"] == "tasks")
        self.assertEqual(task_trace["status"], "timeout")

    def test_context_loader_preserves_seeded_target_document(self) -> None:
        workflow = TimeoutContextWorkflow()
        state = {
            "message": message("load context").model_dump(mode="json"),
            "task_run_id": "run_1",
            "context": {
                "session_id": "chat_1",
                "current_document": {"document_id": "doc_target", "title": "Target doc"},
            },
            "trace": [],
        }

        result = context_loader_node(workflow)(state)

        self.assertEqual(result["context"]["current_document"]["document_id"], "doc_target")

    def test_shadow_persists_step_and_metadata_when_task_run_id_exists(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "read",
                    "object": "tasks",
                    "confidence": 0.91,
                }
            )
        )

        GraphRunner(workflow).run_shadow(
            message("现在有哪些任务"),
            task_run_id="run_1",
            legacy_route={"route": "status", "confidence": 0.95, "needs_clarification": False},
        )

        self.assertEqual(workflow.task_run_service.steps[0]["step_key"], "graph.shadow_command")
        self.assertEqual(workflow.task_run_service.steps[0]["status"], "done")
        self.assertEqual(workflow.task_run_service.metadata[0][0], "run_1")
        self.assertIn("langgraph_shadow", workflow.task_run_service.metadata[0][1])
        shadow = workflow.task_run_service.metadata[0][1]["langgraph_shadow"]
        self.assertEqual(shadow["comparison"]["status"], "match")
        self.assertTrue(shadow["comparison"]["route_match"])
        self.assertEqual(shadow["comparison"]["legacy_route"], "status")
        self.assertEqual(shadow["comparison"]["graph_route"], "status")

    def test_shadow_comparison_flags_route_divergence(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["slides"],
                    "confidence": 0.88,
                }
            )
        )

        GraphRunner(workflow).run_shadow(
            message("整理成文档"),
            task_run_id="run_1",
            legacy_route={"route": "doc", "confidence": 0.96},
        )

        shadow = workflow.task_run_service.metadata[0][1]["langgraph_shadow"]
        comparison = shadow["comparison"]
        self.assertEqual(comparison["status"], "diverged")
        self.assertFalse(comparison["route_match"])
        self.assertFalse(comparison["outputs_match"])
        self.assertIn("route_mismatch", comparison["notes"])

    def test_shadow_interpreter_adopts_legacy_route_when_command_route_is_missing(self) -> None:
        workflow = FakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "read",
                    "object": "workspace",
                    "confidence": 0.92,
                }
            )
        )

        state = GraphRunner(workflow).run_shadow(
            message("what are the current risks"),
            legacy_route={"route": "risks", "confidence": 0.9},
        )

        self.assertEqual(state.command.route, "risks")
        self.assertEqual(state.plan.steps[0].worker, "analysis")

    def test_task_graph_can_deliver_status_reply(self) -> None:
        workflow = ExecutableFakeWorkflow(FakeLLMService(configured=False))

        result = GraphRunner(workflow).run_task_graph(
            message("任务列表"),
            task_run_id="run_1",
            workspace_context="",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "status")
        self.assertIn("当前有 1 项任务", result["reply_preview"])
        self.assertEqual(workflow.task_run_service.steps[-1]["step_key"], "graph.execution")

    def test_task_graph_updates_task_status_from_update_operation(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "update",
                    "object": "task",
                    "target_text": "ppt",
                    "target_status": "done",
                    "confidence": 0.95,
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("mark ppt as done"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "tasks")
        self.assertIn("done", result["reply_preview"])
        saved_analysis = workflow.memory_service.saved_rounds[0]["analysis"]
        self.assertEqual(saved_analysis.tasks[0].status, "done")

    def test_task_graph_applies_structured_task_operations(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "update",
                    "object": "task",
                    "confidence": 0.95,
                    "task_operations": [
                        {
                            "action": "update",
                            "match_hint": {"title": "制作ppt", "owner": "张三"},
                            "task": {
                                "title": "制作ppt",
                                "owner": "张三",
                                "priority": "high",
                                "due_date": "2026-05-10",
                                "status": "draft",
                                "notes": "时间提前",
                            },
                            "reason": "用户调整了截止时间",
                        }
                    ],
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("move the ppt deadline earlier"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "tasks")
        saved_analysis = workflow.memory_service.saved_rounds[0]["analysis"]
        self.assertEqual(saved_analysis.tasks[0].priority, "high")
        self.assertEqual(saved_analysis.tasks[0].due_date, "2026-05-10")

    def test_task_graph_can_deliver_risk_analysis_reply(self) -> None:
        workflow = ExecutableFakeWorkflow(FakeLLMService(configured=False))

        result = GraphRunner(workflow).run_task_graph(
            message("risk list"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=7,
            legacy_route={"route": "risks", "source": "rule_exact", "confidence": 1.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "risks")
        self.assertIn("risks ready", result["reply_preview"])
        self.assertEqual(workflow.analysis_execution.calls[0]["intent"], "risks")
        self.assertEqual(workflow.analysis_execution.calls[0]["active_episode_id"], 7)

    def test_task_graph_can_deliver_help_reply(self) -> None:
        workflow = ExecutableFakeWorkflow(FakeLLMService(configured=False))

        result = GraphRunner(workflow).run_task_graph(
            message("help"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
            legacy_route={"route": "help", "source": "rule_exact", "confidence": 1.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "help")
        self.assertIn("help ready", result["reply_preview"])

    def test_task_graph_can_deliver_chat_reply(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "chat",
                    "route": "unknown",
                    "operation": "chat",
                    "object": "unknown",
                    "reply": "我可以继续帮你整理协作内容。",
                    "confidence": 0.91,
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("随便聊聊今天的协作状态"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
            legacy_route={"route": "unknown", "source": "llm", "confidence": 0.91},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "help")
        self.assertIn("继续帮你整理", result["reply_preview"])

    def test_primary_graph_handles_analysis_and_help_routes(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": kwargs["legacy_route"]["route"], "reply_preview": "ok"}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("风险列表"),
                task_run_id="run_1",
                workspace_context="",
                active_episode_id=None,
                route_decision=RouteDecision(route="risks", source="rule_exact", confidence=1.0),
            )
            help_result = workflow._run_graph_task_command(
                message("help"),
                task_run_id="run_2",
                workspace_context="",
                active_episode_id=None,
                route_decision=RouteDecision(route="help", source="rule_exact", confidence=1.0),
            )

        self.assertEqual(result["mode"], "risks")
        self.assertEqual(calls[0]["legacy_route"]["route"], "risks")
        self.assertEqual(help_result["mode"], "help")
        self.assertEqual(calls[1]["legacy_route"]["route"], "help")

    def test_primary_graph_handles_supported_task_routes(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": "status", "reply_preview": "ok"}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("任务列表"),
                task_run_id="run_1",
                workspace_context="",
                active_episode_id=None,
                route_decision=RouteDecision(route="status", source="rule_exact", confidence=1.0),
            )

        self.assertEqual(result["mode"], "status")
        self.assertEqual(calls[0]["legacy_route"]["route"], "status")

    def test_primary_graph_handles_unknown_routes(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": "help", "reply_preview": "ok"}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("你是谁"),
                task_run_id="run_1",
                workspace_context="",
                active_episode_id=None,
                route_decision=RouteDecision(route="unknown", source="llm", confidence=0.88),
            )

        self.assertEqual(result["mode"], "help")
        self.assertEqual(calls[0]["legacy_route"]["route"], "unknown")
        self.assertFalse(calls[0]["legacy_route"]["needs_clarification"])

    def test_primary_graph_handles_clarification_routes(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": "help", "reply_preview": "please clarify"}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("帮我处理一下"),
                task_run_id="run_1",
                workspace_context="",
                active_episode_id=None,
                route_decision=RouteDecision(
                    route="unknown",
                    source="llm",
                    confidence=0.35,
                    needs_clarification=True,
                    reason="ambiguous request",
                ),
            )

        self.assertEqual(result["mode"], "help")
        self.assertEqual(calls[0]["legacy_route"]["route"], "unknown")
        self.assertTrue(calls[0]["legacy_route"]["needs_clarification"])

    def test_primary_graph_handles_doc_revision_document_selection(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": "doc", "reply_preview": "pick a document", "pending_confirmation": True}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)
        workflow.session_document_service = SimpleNamespace(
            list_documents=lambda session_id: [
                {"document_id": "doc_1", "title": "Alpha", "is_current": True},
                {"document_id": "doc_2", "title": "Beta"},
            ]
        )

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("update the document"),
                task_run_id="run_1",
                workspace_context="workspace",
                active_episode_id=None,
                route_decision=RouteDecision(route="doc", source="llm", confidence=0.92),
            )

        self.assertEqual(result["mode"], "doc")
        self.assertTrue(calls[0]["legacy_route"]["needs_clarification"])
        self.assertIn("Alpha", calls[0]["legacy_route"]["clarification"]["options"][0])
        self.assertIsNone(calls[0]["current_document"])

    def test_primary_graph_passes_resolved_doc_target_to_runner(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        calls: list[dict] = []
        current_doc = {
            "document_id": "doc_1",
            "title": "Alpha",
            "is_current": True,
            "url": "https://example.test/doc_1",
        }
        workflow.session_document_service = SimpleNamespace(
            list_documents=lambda session_id: [current_doc],
            get_current_document=lambda session_id: current_doc,
        )
        workflow.memory_service = SimpleNamespace(build_discussion_block=lambda *args, **kwargs: "latest discussion")

        def run_task_graph(*args, **kwargs):
            calls.append(kwargs)
            return {"mode": "doc", "reply_preview": "ok"}

        workflow.graph_runner = SimpleNamespace(run_task_graph=run_task_graph)

        with (
            patch("app.services.feishu_workflow.settings.langgraph_enabled", True),
            patch("app.services.feishu_workflow.settings.langgraph_shadow_mode", False),
            patch("app.services.feishu_workflow.settings.workflow_engine", "langgraph"),
        ):
            result = workflow._run_graph_task_command(
                message("update current document"),
                task_run_id="run_1",
                workspace_context="workspace",
                active_episode_id=7,
                route_decision=RouteDecision(route="doc", source="llm", confidence=0.92),
            )

        self.assertEqual(result["mode"], "doc")
        self.assertEqual(calls[0]["current_document"]["document_id"], "doc_1")
        self.assertIn("workspace", calls[0]["workspace_context"])

    def test_graph_context_artifacts_loader_returns_old_to_new_artifacts(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)

        class FakeArtifactTaskRunService:
            def list_task_runs(self, *, session_id, limit):
                return [
                    SimpleNamespace(task_run_id="newer"),
                    SimpleNamespace(task_run_id="older"),
                ]

            def get_task_run(self, task_run_id):
                return SimpleNamespace(
                    artifacts=[
                        {
                            "artifact_type": "slides_package",
                            "title": task_run_id,
                            "preview": {"slides": [{"title": task_run_id}]},
                        }
                    ]
                )

        workflow.task_run_service = FakeArtifactTaskRunService()

        artifacts = workflow.graph_context_artifacts_loader("chat_1")

        self.assertEqual([item["title"] for item in artifacts], ["older", "newer"])

    def test_graph_confirmation_resume_is_skipped_when_primary_disabled(self) -> None:
        workflow = FeishuWorkflowService.__new__(FeishuWorkflowService)
        workflow.graph_runner = SimpleNamespace(
            resume_after_confirmation=lambda *args, **kwargs: self.fail("disabled graph should not resume")
        )
        workflow.revision_workflow = SimpleNamespace(
            resume_task_run_after_confirmation=lambda *args, **kwargs: {"mode": "legacy_resume"}
        )

        with patch("app.services.feishu_workflow.settings.langgraph_enabled", False):
            result = workflow.resume_task_run_after_confirmation(
                "run_1",
                confirmation_id="confirm_1",
                answer_value="confirm",
                answered_by="ou_1",
            )

        self.assertEqual(result["mode"], "legacy_resume")

    def test_execution_graph_runs_parallel_ready_workers(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["doc", "slides", "canvas"],
                    "artifact_goals": {
                        "doc": "整理成项目说明文档",
                        "slides": "生成汇报 PPT 大纲",
                        "canvas": "画出协作流程图",
                    },
                    "confidence": 0.95,
                }
            )
        )

        def graph_worker_executor(state, step):
            time.sleep(0.2)
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=True,
                output={"reply_preview": f"{step.worker} done"},
            )

        workflow.graph_worker_executor = graph_worker_executor
        runner = GraphRunner(workflow)
        initial_state = runner._initial_state(
            message("make a product bundle as document, slides, and canvas"),
            task_run_id="run_1",
            workspace_context="",
            legacy_route=None,
        )

        started = time.perf_counter()
        final_payload = runner.execution_graph.invoke(initial_state.model_dump(mode="json"))
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.45)
        self.assertEqual(set(final_payload["worker_results"]), {"doc_generate", "slides_generate", "canvas_generate"})
        self.assertTrue(all(item["ok"] for item in final_payload["worker_results"].values()))

    def test_task_graph_delivers_compound_artifacts(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["doc", "slides", "canvas"],
                    "artifact_goals": {
                        "doc": "整理成项目说明文档",
                        "slides": "生成汇报 PPT 大纲",
                        "canvas": "画出协作流程图",
                    },
                    "confidence": 0.95,
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("make a product bundle as document, slides, and canvas"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=7,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "artifacts")
        self.assertIn("doc ready", result["reply_preview"])
        self.assertIn("slides ready", result["reply_preview"])
        self.assertIn("canvas ready", result["reply_preview"])
        self.assertEqual([item["artifact_type"] for item in result["artifacts"]], ["doc", "slides", "canvas"])
        self.assertEqual(workflow.doc_execution.calls[0]["kwargs"]["active_episode_id"], 7)
        doc_llm_result = workflow.doc_execution.calls[0]["kwargs"]["llm_result"]
        slides_llm_result = workflow.slides_execution.calls[0]["kwargs"]["llm_result"]
        self.assertEqual(doc_llm_result["requested_outputs"], ["doc"])
        self.assertEqual(slides_llm_result["requested_outputs"], ["slides"])
        self.assertEqual(doc_llm_result["plan"]["goal"], "整理成项目说明文档")
        self.assertEqual(slides_llm_result["plan"]["steps"][0]["agent"], "SlidesAgent")

    def test_task_graph_returns_partial_artifacts_without_fallback(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": ["doc", "slides"],
                    "confidence": 0.95,
                }
            )
        )

        def graph_worker_executor(state, step):
            if step.worker == "doc":
                return WorkerResult(
                    step_id=step.step_id,
                    worker=step.worker,
                    ok=True,
                    output={
                        "reply_preview": "doc ready",
                        "artifacts": [{"artifact_type": "doc", "title": "Doc", "url": "/doc"}],
                    },
                )
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=False,
                status="failed",
                error="slides exporter unavailable",
            )

        workflow.graph_worker_executor = graph_worker_executor

        result = GraphRunner(workflow).run_task_graph(
            message("make a document and slides"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "artifacts")
        self.assertIn("doc ready", result["reply_preview"])
        self.assertIn("部分步骤未完成", result["reply_preview"])
        self.assertIn("slides exporter unavailable", result["reply_preview"])
        self.assertEqual([item["artifact_type"] for item in result["artifacts"]], ["doc"])
        execution = workflow.task_run_service.metadata_by_run["run_1"]["langgraph_execution"]
        self.assertFalse(execution["review"]["ok"])
        checks = {item["agent"]: item for item in execution["review"]["checks"]}
        self.assertEqual(checks["ValidatorAgent"]["status"], "failed")
        self.assertEqual(checks["ShieldAgent"]["status"], "passed")
        self.assertIn("slides exporter unavailable", execution["review"]["risks"])

    def test_task_graph_bundles_generated_artifacts_for_delivery(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "delivery",
                    "operation": "generate",
                    "object": "delivery",
                    "requested_outputs": ["slides"],
                    "artifact_goals": {"slides": "make a delivery deck"},
                    "confidence": 0.95,
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("make slides and bundle delivery"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "delivery")
        self.assertEqual([item["artifact_type"] for item in result["artifacts"]], ["slides", "delivery_bundle"])
        self.assertEqual(workflow.delivery_artifact_service.persisted[0]["task_run_id"], "run_1")
        manifest = workflow.delivery_artifact_service.persisted[0]["manifest"]
        self.assertEqual(len(manifest["artifacts"]), 1)
        self.assertEqual(manifest["artifacts"][0]["artifact_type"], "slides")

    def test_task_graph_bundles_existing_context_artifacts_for_delivery(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "delivery",
                    "operation": "generate",
                    "object": "delivery",
                    "requested_outputs": [],
                    "confidence": 0.95,
                }
            )
        )
        workflow.context_artifacts = [
            {
                "artifact_type": "slides_package",
                "title": "Existing deck",
                "url": "/slides-existing",
                "preview": {"slides": [{"title": "Intro"}]},
            }
        ]

        result = GraphRunner(workflow).run_task_graph(
            message("bundle existing artifacts"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "delivery")
        self.assertEqual([item["artifact_type"] for item in result["artifacts"]], ["delivery_bundle"])
        manifest = workflow.delivery_artifact_service.persisted[0]["manifest"]
        self.assertEqual(manifest["artifacts"][0]["title"], "Existing deck")

    def test_task_graph_revises_existing_slides_artifact(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "slides",
                    "operation": "revise",
                    "object": "slides",
                    "confidence": 0.95,
                },
            )
        )
        workflow.context_artifacts = [
            {
                "artifact_type": "slides_package",
                "title": "Deck",
                "preview": {
                    "theme": "Deck",
                    "version": 1,
                    "slides": [
                        {
                            "title": "Intro",
                            "bullets": ["old"],
                            "speaker_notes": "old",
                        }
                    ],
                },
            }
        ]

        result = GraphRunner(workflow).run_task_graph(
            message("revise slide one"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "slides")
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "slides_package")
        self.assertEqual(artifact["preview"]["version"], 2)
        self.assertIn("revise slide one", artifact["preview"]["slides"][0]["speaker_notes"])

    def test_task_graph_revises_existing_canvas_artifact(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "canvas",
                    "operation": "revise",
                    "object": "canvas",
                    "confidence": 0.95,
                }
            )
        )
        workflow.context_artifacts = [
            {
                "artifact_type": "canvas",
                "title": "Flow",
                "preview": {
                    "title": "Flow",
                    "version": 1,
                    "shapes": [
                        {
                            "id": "s1",
                            "type": "text",
                            "text": "old",
                        }
                    ],
                },
            }
        ]

        result = GraphRunner(workflow).run_task_graph(
            message("revise canvas node"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "canvas")
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "canvas")
        self.assertEqual(artifact["preview"]["version"], 2)
        self.assertIn("revise canvas node", artifact["preview"]["shapes"][0]["text"])

    def test_task_graph_asks_before_revising_missing_slides_artifact(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "slides",
                    "operation": "revise",
                    "object": "slides",
                    "confidence": 0.95,
                },
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("revise the deck"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["pending_confirmation"])
        self.assertIn("PPT", result["reply_preview"])

    def test_graph_confirmation_resume_executes_destructive_task_after_answer(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "remove",
                    "object": "task",
                    "target_text": "ppt",
                    "destructive": True,
                    "confidence": 0.94,
                }
            )
        )
        runner = GraphRunner(workflow)

        pending = runner.run_task_graph(
            message("delete ppt task"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertTrue(pending["pending_confirmation"])
        confirmation_id = pending["confirmation_id"]
        metadata = workflow.task_run_service.get_task_run_metadata("run_1")
        self.assertIn("resume_after_graph_confirmation", metadata)

        resumed = runner.resume_after_confirmation(
            "run_1",
            confirmation_id=confirmation_id,
            answer_value="confirm",
            answered_by="ou_1",
        )

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["mode"], "tasks")
        self.assertTrue(workflow.memory_service.saved_rounds)
        self.assertNotIn("resume_after_graph_confirmation", workflow.task_run_service.get_task_run_metadata("run_1"))
        self.assertEqual(workflow.task_run_service.steps[-1]["step_key"], "graph.confirmation_resume")
        self.assertEqual(workflow.task_run_service.steps[-1]["status"], "done")
        execution = workflow.task_run_service.metadata_by_run["run_1"]["langgraph_execution"]
        checks = {item["agent"]: item for item in execution["review"]["checks"]}
        self.assertEqual(checks["ValidatorAgent"]["status"], "passed")
        self.assertEqual(checks["ShieldAgent"]["status"], "passed")
        review_trace = next(item for item in execution["trace"] if item.get("node") == "graph.reviewer")
        self.assertEqual(
            [item["agent"] for item in review_trace["checks"]],
            ["ValidatorAgent", "ShieldAgent"],
        )

    def test_confirmation_resume_keeps_deterministic_guard_checks(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "generate",
                    "object": "workspace",
                    "requested_outputs": [],
                    "confidence": 0.94,
                }
            )
        )
        runner = GraphRunner(workflow)

        pending = runner.run_task_graph(
            message("make something"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertTrue(pending["pending_confirmation"])
        resumed = runner.resume_after_confirmation(
            "run_1",
            confirmation_id=pending["confirmation_id"],
            answer_value="confirm",
            answered_by="ou_1",
        )

        self.assertIsNotNone(resumed)
        self.assertTrue(resumed["pending_confirmation"])
        self.assertEqual(workflow.reply_sender.delivered, [])

    def test_graph_doc_selection_resume_uses_answered_document(self) -> None:
        selected_doc = {"document_id": "doc_2", "title": "Beta", "version": 2}
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "revise",
                    "object": "doc",
                    "route": "doc",
                    "confidence": 0.96,
                    "reason": "revise selected document",
                }
            )
        )
        workflow._resolve_target_document_for_instruction = (  # type: ignore[attr-defined]
            lambda session_id, instruction: selected_doc if "Beta" in instruction else None
        )
        workflow._join_context_blocks = (  # type: ignore[attr-defined]
            lambda *blocks: "\n\n".join(str(block).strip() for block in blocks if str(block or "").strip())
        )
        runner = GraphRunner(workflow)

        pending = runner.run_task_graph(
            message("更新这份文档"),
            task_run_id="run_doc",
            workspace_context="workspace",
            active_episode_id=3,
            legacy_route={
                "route": "doc",
                "confidence": 0.96,
                "needs_clarification": True,
                "clarification_question": "请选择文档",
                "clarification": {
                    "question": "请选择文档",
                    "reason": "document selection required",
                    "options": ["Alpha", "Beta"],
                    "blocking": True,
                },
            },
        )

        self.assertTrue(pending["pending_confirmation"])
        resumed = runner.resume_after_confirmation(
            "run_doc",
            confirmation_id=pending["confirmation_id"],
            answer_value="Beta",
            answered_by="ou_1",
        )

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["mode"], "doc")
        self.assertEqual(workflow.doc_execution.calls[-1]["kwargs"]["target_document"]["document_id"], "doc_2")
        self.assertIn("Beta", workflow.doc_execution.calls[-1]["kwargs"]["workspace_context"])

    def test_graph_resume_can_return_chat_reply(self) -> None:
        workflow = ExecutableFakeWorkflow(FakeLLMService())
        runner = GraphRunner(workflow)
        state = runner._initial_state(
            message("chat only"),
            task_run_id="run_1",
            workspace_context="workspace",
            legacy_route=None,
        ).model_copy(
            update={
                "command": WorkspaceCommand(
                    route="unknown",
                    mode="chat",
                    operation="chat",
                    object="unknown",
                    reason="chat request",
                    confidence=0.95,
                )
            }
        )
        workflow.task_run_service.metadata_by_run["run_1"] = {
            "resume_after_graph_confirmation": {
                "confirmation_id": "confirm_1",
                "state": state.model_dump(mode="json"),
            },
            "resume_after_confirmation": {
                "confirmation_id": "confirm_1",
                "intent": "help",
            },
        }

        result = runner.resume_after_confirmation(
            "run_1",
            confirmation_id="confirm_1",
            answer_value="confirm",
            answered_by="ou_1",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["mode"], "help")
        self.assertIn("help ready", result["reply_preview"])
        metadata = workflow.task_run_service.get_task_run_metadata("run_1")
        self.assertNotIn("resume_after_graph_confirmation", metadata)
        self.assertNotIn("resume_after_confirmation", metadata)
        self.assertEqual(workflow.task_run_service.steps[-1]["status"], "done")

    def test_cancel_graph_confirmation_preserves_session_id(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "operation": "remove",
                    "object": "task",
                    "target_text": "ppt",
                    "destructive": True,
                    "confidence": 0.94,
                }
            )
        )
        runner = GraphRunner(workflow)

        pending = runner.run_task_graph(
            message("delete ppt task"),
            task_run_id="run_1",
            workspace_context="workspace",
            active_episode_id=None,
        )

        result = runner.resume_after_confirmation(
            "run_1",
            confirmation_id=pending["confirmation_id"],
            answer_value="cancel",
            answered_by="ou_1",
        )

        self.assertEqual(result["session_id"], "chat_1")
        self.assertEqual(result["mode"], "graph_confirmation_cancelled")


if __name__ == "__main__":
    unittest.main()
