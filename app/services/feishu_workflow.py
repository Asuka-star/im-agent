import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.doc_api import FeishuDocAPI
from app.feishu.message_api import FeishuMessageAPI
from app.feishu.user_api import FeishuUserAPI
from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext, FeishuMessageLifecycleContext
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.interaction import InteractionService
from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.tools.canvas_tool import CanvasTool
from app.services.delivery_artifact_service import DeliveryArtifactService
from app.services.tools.delivery_tool import DeliveryTool
from app.services.document_package_builder import DocumentPackageBuilder
from app.services.tools.doc_tool import DocTool
from app.services.execution_planner import ExecutionPlanner, RequestProtocol
from app.services.llm import LLMService
from app.services.memory_service import MemoryService
from app.services.next_action_service import ContextualNextActionService
from app.services.office_artifact_service import OfficeArtifactService
from app.services.presentation_artifact_service import PresentationArtifactService
from app.services.tools.presentation_tool import PresentationTool
from app.services.response_formatter import ResponseFormatter
from app.services.request_router import RequestRouter, RouteDecision
from app.services.session_document_service import SessionDocumentService
from app.services.tools.task_operation_tool import TaskOperationTool
from app.services.task_run_service import TaskRunService
from app.services.tools.workbench_revision_tool import WorkbenchRevisionTool
from app.services.graph.runner import GraphRunner
from app.services.workflow.document_tasks import (
    merge_status_task_sources,
    task_items_from_llm_payload,
    tasks_from_document_snapshot,
)
from app.services.workflow.doc_execution import WorkflowDocExecution
from app.services.workflow.entrypoint import WorkflowEntrypoint
from app.services.workflow.execution import WorkflowExecutionRunner
from app.services.workflow.fallback import WorkflowFallbackHandler
from app.services.workflow.persistence import WorkflowResultPersistence
from app.services.workflow.replies import WorkflowReplySender
from app.services.workflow.revisions import WorkbenchRevisionWorkflow
from app.services.workflow.analysis_execution import WorkflowAnalysisExecution
from app.services.workflow.canvas_execution import WorkflowCanvasExecution
from app.services.workflow.slides_execution import WorkflowSlidesExecution
from app.services.workflow.status_execution import WorkflowStatusExecution
from app.services.workflow.task_intent_execution import WorkflowTaskIntentExecution
from app.services.text_analysis import (
    apply_discussion_updates,
    build_next_actions,
    build_summary,
    extract_tasks,
    infer_risks,
    normalize_tasks,
)

logger = logging.getLogger(__name__)


class FeishuWorkflowService:
    """Handles buffered collaboration and LLM-first mentioned requests."""

    def __init__(self) -> None:
        self.orchestrator = AgentOrchestrator()
        self.message_api = FeishuMessageAPI()
        self.canvas_artifact_service = CanvasArtifactService()
        self.canvas_tool = CanvasTool(artifact_service=self.canvas_artifact_service)
        self.delivery_artifact_service = DeliveryArtifactService()
        self.document_package_builder = DocumentPackageBuilder()
        self.doc_api = FeishuDocAPI()
        self.user_api = FeishuUserAPI()
        self.memory_service = MemoryService()
        self.office_artifact_service = OfficeArtifactService()
        self.presentation_artifact_service = PresentationArtifactService()
        self.presentation_tool = PresentationTool(artifact_service=self.presentation_artifact_service)
        self.session_document_service = SessionDocumentService()
        self.task_run_service = TaskRunService()
        self.delivery_tool = DeliveryTool(
            delivery_artifact_service=self.delivery_artifact_service,
            task_run_service=self.task_run_service,
            message_api=self.message_api,
        )
        self.workbench_revision_tool = WorkbenchRevisionTool(
            task_run_service=self.task_run_service,
            memory_service=self.memory_service,
        )
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()
        self.next_action_service = ContextualNextActionService(llm_service=self.llm_service, enable_llm=False)
        self.execution_planner = ExecutionPlanner()
        self.request_router = RequestRouter()
        self.response_formatter = ResponseFormatter()
        self.graph_runner = GraphRunner(self)
        self.analysis_execution = WorkflowAnalysisExecution(self)
        self.canvas_execution = WorkflowCanvasExecution(self)
        self.slides_execution = WorkflowSlidesExecution(self)
        self.status_execution = WorkflowStatusExecution(self)
        self.task_intent_execution = WorkflowTaskIntentExecution(self)
        self.entrypoint = WorkflowEntrypoint(self)
        self.doc_execution = WorkflowDocExecution(self)
        self.execution_runner = WorkflowExecutionRunner(self)
        self.fallback_handler = WorkflowFallbackHandler(self)
        self.result_persistence = WorkflowResultPersistence(self)
        self.reply_sender = WorkflowReplySender(self)
        self.revision_workflow = WorkbenchRevisionWorkflow(self)

    def _doc_tool(self) -> DocTool:
        return DocTool(
            doc_api=self.doc_api,
            session_document_service=self.session_document_service,
        )

    def handle_message(self, message: FeishuMessageContext) -> dict:
        return self.entrypoint.handle_message(message)

    def handle_message_lifecycle(self, event: FeishuMessageLifecycleContext) -> dict:
        if event.event_type == "im.message.recalled_v1":
            updated = self.memory_service.mark_message_recalled(
                message_id=event.message_id,
                chat_id=event.chat_id,
                recall_time=event.recall_time,
                recall_type=event.recall_type,
            )
            logger.info(
                "Handled Feishu recalled message event: message_id=%s updated=%s recall_type=%s",
                event.message_id,
                updated,
                event.recall_type,
            )
            impact = self._mark_message_lifecycle_impacts(event, action="recalled")
            return {"mode": "message_recalled", "message_id": event.message_id, "updated": updated, "impact": impact}

        updated = False
        if event.raw_text:
            updated = self.memory_service.update_user_message_content(
                message_id=event.message_id,
                content=event.raw_text,
                chat_id=event.chat_id,
            )
        logger.info(
            "Handled Feishu updated message event: message_id=%s updated=%s has_text=%s",
            event.message_id,
            updated,
            bool(event.raw_text),
        )
        impact = self._mark_message_lifecycle_impacts(event, action="updated") if updated else {}
        return {"mode": "message_updated", "message_id": event.message_id, "updated": updated, "impact": impact}

    def _mark_message_lifecycle_impacts(self, event: FeishuMessageLifecycleContext, *, action: str) -> dict:
        message_info = self.memory_service.get_message_lifecycle_info(event.message_id)
        session_id = str(message_info.get("session_id") or event.chat_id or "").strip()
        if not session_id:
            return {"task_run_ids": [], "document_ids": [], "reason": "message_session_unknown"}

        episode_id = message_info.get("episode_id")
        task_run_ids: list[str] = []
        for task_run in self.task_run_service.list_task_runs(session_id=session_id, limit=100):
            if task_run.trigger_message_id == event.message_id:
                task_run_ids.append(task_run.task_run_id)

        reason = f"源消息已{('撤回' if action == 'recalled' else '编辑')}，相关产物需要复核。"
        memory_impact = self.memory_service.mark_episode_outputs_source_dirty(
            session_id=session_id,
            episode_id=episode_id if isinstance(episode_id, int) else None,
            message_id=event.message_id,
            event_type=event.event_type,
            reason=reason,
        )
        dirty_documents = self.session_document_service.mark_documents_source_dirty(
            session_id,
            message_id=event.message_id,
            episode_id=episode_id if isinstance(episode_id, int) else None,
            task_run_ids=list(task_run_ids),
            event_type=event.event_type,
            reason=reason,
        )
        for document in dirty_documents:
            task_run_id = str(document.get("task_run_id") or "").strip()
            if task_run_id and task_run_id not in task_run_ids:
                task_run_ids.append(task_run_id)

        task_cleanup_count = 0
        if action == "recalled":
            task_cleanup_count = self._remove_lifecycle_source_tasks_from_current_snapshot(
                session_id=session_id,
                source_text=str(message_info.get("original_content") or message_info.get("content") or ""),
                episode_id=episode_id if isinstance(episode_id, int) else None,
                message_id=event.message_id,
            )

        for task_run_id in task_run_ids:
            self.task_run_service.merge_task_run_metadata(
                task_run_id,
                {
                    "source_dirty": True,
                    "source_dirty_message_id": event.message_id,
                    "source_dirty_event_type": event.event_type,
                    "source_dirty_reason": reason,
                },
            )
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="source_lifecycle_notice",
                title="源消息发生变更",
                step_type="source_lifecycle",
                status="needs_review",
                output_payload={
                    "message_id": event.message_id,
                    "event_type": event.event_type,
                    "action": action,
                    "reason": reason,
                },
            )

        return {
            "task_run_ids": task_run_ids,
            "document_ids": [str(document.get("document_id") or "") for document in dirty_documents],
            "memory_count": memory_impact.get("memory_count", 0),
            "chunk_count": memory_impact.get("chunk_count", 0),
            "task_cleanup_count": task_cleanup_count,
            "reason": reason,
        }

    def _remove_lifecycle_source_tasks_from_current_snapshot(
        self,
        *,
        session_id: str,
        source_text: str,
        episode_id: int | None,
        message_id: str,
    ) -> int:
        source_tasks = normalize_tasks(extract_tasks(source_text))
        if not source_tasks:
            return 0
        try:
            current_tasks = [self._task_item_from_any(task) for task in self.memory_service.get_current_tasks(session_id)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current tasks for lifecycle cleanup: session_id=%s error=%s", session_id, exc)
            return 0
        remaining_tasks = [task for task in current_tasks if task is not None]
        removed: list[TaskItem] = []
        for source_task in source_tasks:
            target_index = self._find_lifecycle_task_match(remaining_tasks, source_task)
            if target_index is None:
                continue
            removed.append(remaining_tasks.pop(target_index))
        if not removed:
            return 0
        risks = infer_risks(remaining_tasks)
        analysis = AnalyzeResponse(
            session_id=session_id,
            summary="源消息已撤回，已从当前任务快照移除对应任务。",
            tasks=remaining_tasks,
            risks=risks,
            next_actions=build_next_actions(remaining_tasks, risks),
            agent_traces=[AgentTrace(agent="memory", summary="removed tasks from recalled source message")],
        )
        try:
            self.memory_service.save_round(
                session_id=session_id,
                analysis=analysis,
                episode_id=episode_id,
                source_message_id=message_id,
                embed=False,
                async_embed=False,
                preserve_unmatched_previous=False,
            )
            logger.info(
                "Removed source-dirty current task(s) after message recall: session_id=%s message_id=%s removed=%s",
                session_id,
                message_id,
                len(removed),
            )
            return len(removed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist lifecycle task cleanup: session_id=%s message_id=%s error=%s", session_id, message_id, exc)
            return 0

    def _find_lifecycle_task_match(self, current_tasks: list[TaskItem], source_task: TaskItem) -> int | None:
        title_matches = [
            index
            for index, task in enumerate(current_tasks)
            if self._lifecycle_task_title_matches(task, source_task)
        ]
        if not title_matches:
            return None
        owner_matches = [
            index
            for index in title_matches
            if self._lifecycle_task_owner_matches(current_tasks[index], source_task)
        ]
        if len(owner_matches) == 1:
            return owner_matches[0]
        if len(title_matches) == 1:
            return title_matches[0]
        return None

    @staticmethod
    def _task_item_from_any(task: Any) -> TaskItem | None:
        if isinstance(task, TaskItem):
            return task
        title = str(getattr(task, "title", "") or "").strip()
        if not title:
            return None
        return TaskItem(
            title=title,
            owner=str(getattr(task, "owner", "") or "TBD"),
            priority=str(getattr(task, "priority", "") or "medium"),
            due_date=str(getattr(task, "due_date", "") or "TBD"),
            status=str(getattr(task, "status", "") or "draft"),
            notes=str(getattr(task, "notes", "") or ""),
        )

    @staticmethod
    def _lifecycle_task_title_matches(current_task: TaskItem, source_task: TaskItem) -> bool:
        source_probe = " ".join([source_task.title, source_task.notes or ""]).strip()
        return (
            TaskOperationTool.line_matches_task_title(source_probe, current_task.title)
            or TaskOperationTool.line_matches_task_title(current_task.title, source_task.title)
        )

    @staticmethod
    def _lifecycle_task_owner_matches(current_task: TaskItem, source_task: TaskItem) -> bool:
        current_owner = FeishuWorkflowService._compact_task_label(current_task.owner)
        source_owner = FeishuWorkflowService._compact_task_label(source_task.owner)
        if not current_owner or current_owner == "tbd" or not source_owner or source_owner == "tbd":
            return False
        return current_owner == source_owner or current_owner in source_owner or source_owner in current_owner

    @staticmethod
    def _compact_task_label(value: str | None) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    def _ensure_sender_alias(self, message: FeishuMessageContext) -> None:
        if self.memory_service.get_alias_display_name(message.session_id, message.sender_id):
            return

        display_name = None
        try:
            display_name = self.user_api.get_user_display_name(
                user_id=message.sender_user_id,
                open_id=message.sender_open_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve sender display name for %s: %s", message.sender_id, exc)

        if not display_name:
            return

        self.memory_service.upsert_user_alias(
            message.session_id,
            display_name=display_name,
            user_id=message.sender_user_id,
            open_id=message.sender_open_id,
            union_id=message.sender_union_id,
        )

    def _handle_mentioned_request(self, message: FeishuMessageContext, *, task_run_id: str | None = None) -> dict:
        return self.entrypoint.handle_mentioned_request(message, task_run_id=task_run_id)

    def _build_workspace_context_for_message(
        self,
        message: FeishuMessageContext,
        *,
        active_episode_id: int | None,
        include_semantic_search: bool,
        profile: str = "general",
    ) -> str:
        build_workspace_context = getattr(self.memory_service, "build_workspace_context", None)
        if callable(build_workspace_context):
            context = build_workspace_context(
                message.session_id,
                profile=profile,
                include_pending=True,
                exclude_message_id=message.message_id,
                query_text=message.text,
                include_semantic_search=include_semantic_search,
                episode_id=active_episode_id,
            )
        else:
            context = ""
        team_context = self._build_team_context_for_message(
            message,
            include_semantic_search=include_semantic_search,
        )
        return self._join_context_blocks(context, team_context)

    def _build_team_context_for_message(
        self,
        message: FeishuMessageContext,
        *,
        include_semantic_search: bool = False,
    ) -> str:
        if message.chat_type != "p2p":
            return ""
        return self.memory_service.build_team_workspace_context(
            self._team_id_for_message(message),
            current_session_id=message.session_id,
            query_text=message.text,
            include_semantic_search=include_semantic_search,
        )

    def _route_request(self, instruction: str) -> RouteDecision:
        return self.request_router.route(
            instruction,
            llm_service=self.llm_service,
        )

    def _should_run_graph_shadow(self) -> bool:
        return bool(settings.langgraph_enabled and settings.langgraph_shadow_mode)

    def _should_run_graph_primary(self) -> bool:
        return bool(
            settings.langgraph_enabled
            and not settings.langgraph_shadow_mode
            and str(settings.workflow_engine or "").strip().lower() == "langgraph"
        )

    def _run_graph_shadow(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str | None,
        workspace_context: str,
        route_decision: RouteDecision,
    ) -> None:
        if not self._should_run_graph_shadow():
            return
        try:
            self.graph_runner.run_shadow(
                message,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                legacy_route={
                    "route": route_decision.route,
                    "source": route_decision.source,
                    "confidence": route_decision.confidence,
                    "needs_clarification": route_decision.needs_clarification,
                    "reason": route_decision.reason,
                    "requested_outputs": list(route_decision.requested_outputs),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LangGraph shadow mode failed without interrupting legacy workflow: message_id=%s error=%s",
                message.message_id,
                exc,
            )

    def graph_context_artifacts_loader(self, session_id: str, task_run_id: str | None = None) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        try:
            runs = self.task_run_service.list_task_runs(session_id=session_id, limit=5)
            for run in reversed(runs):
                if task_run_id and run.task_run_id == task_run_id:
                    continue
                detail = self.task_run_service.get_task_run(run.task_run_id)
                if detail is None:
                    continue
                for artifact in detail.artifacts:
                    payload = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
                    artifacts.append(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangGraph artifact context loading failed: session_id=%s error=%s", session_id, exc)
            return []
        return artifacts[-20:]

    def _run_graph_task_command(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str | None,
        workspace_context: str,
        active_episode_id: int | None,
        route_decision: RouteDecision | None,
    ) -> dict | None:
        if not self._should_run_graph_primary():
            return None
        if route_decision is None and not self.llm_service.is_configured():
            return None
        if route_decision is not None and not self._graph_primary_supports_route(route_decision):
            logger.info(
                "LangGraph primary skipped unsupported legacy route: message_id=%s route=%s",
                message.message_id,
                route_decision.route,
            )
            return None
        current_document = None
        graph_workspace_context = workspace_context
        graph_clarification = None
        legacy_route = None
        if route_decision is not None and route_decision.route == "doc":
            current_document = self._resolve_target_document_for_instruction(message.session_id, message.text)
            graph_clarification = self._build_document_selection_clarification(
                message,
                route_decision,
                target_document=current_document,
            )
            if graph_clarification is None:
                graph_workspace_context = self._workspace_context_for_route(
                    route_decision,
                    message,
                    workspace_context,
                    active_episode_id=active_episode_id,
                    target_document=current_document,
                )
        if route_decision is not None:
            legacy_route = {
                "route": route_decision.route,
                "source": route_decision.source,
                "confidence": route_decision.confidence,
                "needs_clarification": route_decision.needs_clarification or graph_clarification is not None,
                "clarification_question": graph_clarification.get("question") if graph_clarification else None,
                "clarification": graph_clarification,
                "reason": graph_clarification.get("reason") if graph_clarification else route_decision.reason,
                "requested_outputs": list(route_decision.requested_outputs),
            }
        try:
            if route_decision is None:
                logger.info("LangGraph primary direct mode started: message_id=%s", message.message_id)
            return self.graph_runner.run_task_graph(
                message,
                task_run_id=task_run_id,
                workspace_context=graph_workspace_context,
                active_episode_id=active_episode_id,
                current_document=current_document,
                legacy_route=legacy_route,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LangGraph primary mode failed, falling back to legacy workflow: message_id=%s error=%s",
                message.message_id,
                exc,
            )
            return None

    @staticmethod
    def _graph_primary_supports_route(route_decision: RouteDecision) -> bool:
        route = str(route_decision.route or "").strip().lower()
        requested_outputs = {
            str(item or "").strip().lower()
            for item in route_decision.requested_outputs
        }
        if route_decision.needs_clarification:
            return True
        if route == "unknown":
            return True
        if route in {"status", "tasks", "summary", "risks", "help", "doc", "slides", "canvas", "delivery"}:
            return True
        return bool(requested_outputs & {"doc", "slides", "canvas"})

    def _should_run_dag_planner(self, route_decision: RouteDecision) -> bool:
        if route_decision.needs_clarification:
            return True
        if route_decision.route == "unknown":
            return True
        if len(route_decision.requested_outputs) > 1:
            return True
        return False

    def _route_decision_from_dag_result(
        self,
        result: dict,
        *,
        fallback: RouteDecision,
    ) -> RouteDecision:
        requested_outputs = self.execution_planner.requested_output_list(result)
        if not requested_outputs:
            requested_outputs = self._requested_outputs_from_plan_result(result)
        if requested_outputs:
            primary_output = requested_outputs[0]
            operation = self.execution_planner.normalize_operation(result.get("operation") or result.get("action"))
            result["operation"] = "update" if operation == "update" else "create"
            result["object"] = primary_output
            result["route"] = primary_output
            if not result.get("requested_outputs"):
                result["requested_outputs"] = list(requested_outputs)

        protocol = self.execution_planner.normalize_request_protocol(result)
        if not requested_outputs and protocol.route in {"doc", "slides", "canvas"}:
            requested_outputs = [protocol.route]

        confidence = self._coerce_confidence(result.get("confidence"), default=fallback.confidence or 0.65)
        clarification = self.execution_planner.extract_clarification_request(result)
        needs_clarification = bool(clarification and clarification["blocking"]) or protocol.route == "unknown"
        return RouteDecision(
            route=protocol.route or fallback.route,
            source="llm_dag",
            confidence=confidence,
            needs_clarification=needs_clarification,
            reason=str(result.get("reason") or fallback.reason or "").strip(),
            requested_outputs=tuple(requested_outputs),
        )

    def _requested_outputs_from_plan_result(self, result: dict) -> list[str]:
        plan = result.get("plan") if isinstance(result, dict) else None
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list):
            return []
        outputs: list[str] = []
        step_outputs = {
            "sync_doc": "doc",
            "generate_slides": "slides",
            "generate_canvas": "canvas",
        }
        for item in steps:
            if not isinstance(item, dict):
                continue
            step_type = self.execution_planner.normalize_plan_step_type(str(item.get("type") or item.get("step_type") or ""))
            output = step_outputs.get(step_type)
            if output and output not in outputs:
                outputs.append(output)
        return outputs

    @staticmethod
    def _coerce_confidence(value: object, *, default: float = 0.0) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = default
        return max(0.0, min(confidence, 1.0))

    def _apply_route_decision_to_llm_result(self, llm_result: dict, route_decision: RouteDecision) -> None:
        protocol = self._protocol_from_route_decision(route_decision)
        if protocol is None:
            return
        existing_operation = self.execution_planner.normalize_operation(llm_result.get("operation") or llm_result.get("action"))
        existing_object = self.execution_planner.normalize_object(llm_result.get("object") or llm_result.get("target"))
        if (
            route_decision.route in {"doc", "slides", "canvas"}
            and existing_operation
            and existing_object == route_decision.route
        ):
            protocol = RequestProtocol(
                operation=existing_operation,
                object=existing_object,
                route=route_decision.route,
            )
        self.execution_planner.store_request_protocol(llm_result, protocol)
        if route_decision.requested_outputs:
            llm_result["requested_outputs"] = list(route_decision.requested_outputs)

    def _resolve_llm_result_for_route(
        self,
        route_decision: RouteDecision,
        workspace_context: str,
        instruction: str,
    ) -> dict:
        route = route_decision.route
        if route == "status":
            return self._minimal_llm_result("read", "tasks", "status", instruction)
        if route == "help":
            return self._minimal_llm_result("help", "workspace", "help", instruction)
        if route == "slides":
            return self._minimal_llm_result("create", "slides", "slides", instruction)
        if route in {"summary", "tasks", "risks"}:
            try:
                return self.llm_service.resolve_analysis_request(workspace_context, instruction, route)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Specialized analysis request failed, falling back to workspace resolver: %s", exc)
        if route == "doc":
            try:
                return self.llm_service.resolve_doc_request(workspace_context, instruction)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Specialized doc request failed, falling back to workspace resolver: %s", exc)
        if route == "canvas":
            return self._minimal_llm_result("create", "canvas", "canvas", instruction)
        return self.llm_service.resolve_workspace_request(workspace_context, instruction)

    def _minimal_llm_result(self, operation: str, object_name: str, route: str, instruction: str) -> dict:
        return {
            "operation": operation,
            "object": object_name,
            "route": route,
            "reason": "已由前置路由确定执行类型",
            "plan": {
                "goal": instruction[:80],
                "steps": [
                    {
                        "id": "step_1",
                        "type": self.execution_planner.required_step_for_protocol(
                            RequestProtocol(operation=operation, object=object_name, route=route)
                        ),
                        "title": self.execution_planner.default_plan_step_title(
                            self.execution_planner.required_step_for_protocol(
                                RequestProtocol(operation=operation, object=object_name, route=route)
                            ),
                            route,
                        ),
                        "depends_on": [],
                    }
                ],
            },
        }

    def _should_run_memory_gate(self, route_decision: RouteDecision, instruction: str) -> bool:
        if route_decision.route in {"status", "help"}:
            return False
        text = str(instruction or "").strip()
        historical_keywords = (
            "之前",
            "以前",
            "上次",
            "上轮",
            "历史",
            "过去",
            "早些时候",
            "前面",
            "为什么",
            "变化",
            "对比",
            "相比",
            "还记得",
        )
        return any(keyword in text for keyword in historical_keywords)

    def _workspace_context_for_route(
        self,
        route_decision: RouteDecision,
        message: FeishuMessageContext,
        workspace_context: str,
        *,
        active_episode_id: int | None,
        target_document: dict | None = None,
    ) -> str:
        lifecycle_context = None
        if route_decision.route in {"doc", "slides", "canvas", "delivery"}:
            lifecycle_context = self._build_workspace_context_for_message(
                message,
                active_episode_id=active_episode_id,
                include_semantic_search=False,
                profile="lifecycle",
            )
            if not lifecycle_context:
                lifecycle_context = workspace_context
        if route_decision.route == "doc":
            return self._build_doc_update_context(
                message,
                lifecycle_context or workspace_context,
                active_episode_id=active_episode_id,
                target_document=target_document,
            )
        if route_decision.route in {"slides", "canvas", "delivery"}:
            return self._build_artifact_lifecycle_context(message, lifecycle_context or workspace_context)
        return workspace_context

    def _build_artifact_lifecycle_context(self, message: FeishuMessageContext, workspace_context: str) -> str:
        try:
            current_doc = self.session_document_service.get_current_document(message.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document context for artifact generation: %s", exc)
            current_doc = None
        doc_context = DocTool.format_current_document_context(current_doc if isinstance(current_doc, dict) else None)
        if not doc_context:
            return workspace_context
        guidance = (
            "[需求生命周期产物策略]\n"
            "- 当前协作文档是从 IM 讨论沉淀出的正式需求/方案上下文。\n"
            "- 生成演示稿、流程图或交付包时优先围绕文档中的背景、痛点、核心需求、产品流程、技术方案、风险与里程碑展开。\n"
            "- 任务分工只作为实施计划支撑，不作为汇报主线。"
        )
        return self._join_context_blocks(workspace_context, doc_context, guidance)

    def _build_doc_update_context(
        self,
        message: FeishuMessageContext,
        workspace_context: str,
        *,
        active_episode_id: int | None,
        target_document: dict | None = None,
    ) -> str:
        try:
            current_doc = target_document or self.session_document_service.get_current_document(message.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document context for IM doc update: %s", exc)
            current_doc = None

        latest_discussion = self.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        )
        update_guidance = ""
        if current_doc and current_doc.get("document_id"):
            update_guidance = (
                "[文档更新策略]\n"
                "- 先比较“当前协作文档”和“本次待同步讨论”，识别新增、修改或删除的信息。\n"
                "- 对发生变化的章节，输出该章节更新后的完整内容，保留仍然有效的既有条目。\n"
                "- 不要把普通“更新文档”理解成重新生成一份泛泛总结。\n"
                "- 只有当本次讨论明确给出负责人、截止时间、状态或风险时，才同步到对应章节；不要根据今天日期、发言人、通用项目阶段自行补排期或负责人。\n"
                "- 如果没有实质变化，可以返回与当前文档一致的章节内容。"
            )

        latest_block = ""
        if latest_discussion:
            latest_block = latest_discussion.replace("[近期群聊讨论]", "[本次待同步讨论]", 1)

        return self._join_context_blocks(
            workspace_context,
            DocTool.format_current_document_context(current_doc),
            latest_block,
            update_guidance,
        )

    def _build_document_selection_clarification(
        self,
        message: FeishuMessageContext,
        route_decision: RouteDecision,
        *,
        target_document: dict | None = None,
    ) -> dict | None:
        if route_decision.route != "doc":
            return None
        if target_document is not None:
            return None
        if not self._looks_like_doc_revision_request(message.text):
            return None
        try:
            documents = self.session_document_service.list_documents(message.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to list session documents for clarification: %s", exc)
            return None
        if len(documents) <= 1:
            return None

        options: list[str] = []
        for item in documents[:4]:
            label = self._document_option_label(item)
            if label:
                options.append(label)
        if not options:
            return None
        return {
            "question": "当前会话里已经有多份协作文档，你希望我更新哪一份？",
            "reason": "这次消息看起来是在继续修订文档，但文本里还没有明确指出目标文档。",
            "options": options,
            "blocking": True,
        }

    def _looks_like_doc_revision_request(self, instruction: str) -> bool:
        text = str(instruction or "").strip().lower()
        if not text:
            return False
        revision_keywords = (
            "更新",
            "修订",
            "修改",
            "补充",
            "完善",
            "调整",
            "润色",
            "改一下",
            "改下",
            "再改",
            "继续改",
            "补一下",
            "update",
            "revise",
            "edit",
            "modify",
            "refresh",
        )
        return any(keyword in text for keyword in revision_keywords)

    def _resolve_target_document_for_instruction(
        self,
        session_id: str,
        instruction: str,
    ) -> dict | None:
        text = str(instruction or "").strip()
        if not text:
            return None
        try:
            documents = self.session_document_service.list_documents(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load session documents for target matching: %s", exc)
            return None
        if not documents:
            return None

        normalized_text = self._normalize_document_match_text(text)
        relative_match = self._resolve_relative_document_reference(normalized_text, documents)
        if relative_match is not None:
            return relative_match
        for document in documents:
            document_id = str(document.get("document_id") or "").strip().lower()
            title = str(document.get("title") or "").strip()
            normalized_title = self._normalize_document_match_text(title)
            option_label = self._normalize_document_match_text(self._document_option_label(document))
            if document_id and document_id in normalized_text:
                return document
            if normalized_title and normalized_title in normalized_text:
                return document
            if option_label and option_label in normalized_text:
                return document
        return None

    def _resolve_relative_document_reference(
        self,
        normalized_text: str,
        documents: list[dict[str, Any]],
    ) -> dict | None:
        if not normalized_text or not documents:
            return None

        if any(
            marker in normalized_text
            for marker in (
                "当前文档",
                "当前这份",
                "最新文档",
                "最新这份",
                "currentdoc",
                "currentdocument",
                "latestdoc",
                "latestdocument",
            )
        ):
            current = next((item for item in documents if item.get("is_current")), None)
            return current or documents[0]

        if any(
            marker in normalized_text
            for marker in (
                "上一份",
                "上一版",
                "上一个文档",
                "前一份",
                "前一个文档",
                "previousdoc",
                "previousdocument",
                "lastdoc",
            )
        ):
            return documents[1] if len(documents) > 1 else documents[0]

        ordinal_match = re.search(r"第(\d+)份", normalized_text)
        if ordinal_match:
            try:
                ordinal = int(ordinal_match.group(1))
            except ValueError:
                ordinal = 0
            if ordinal > 0 and ordinal <= len(documents):
                return documents[ordinal - 1]

        version_match = re.search(r"v(\d+)", normalized_text)
        if version_match:
            target_version = int(version_match.group(1))
            for document in documents:
                try:
                    if int(document.get("version") or 0) == target_version:
                        return document
                except (TypeError, ValueError):
                    continue
        return None

    def _document_option_label(self, document: dict) -> str:
        title = str(document.get("title") or "").strip() or "未命名文档"
        suffix: list[str] = []
        version = document.get("version")
        if version:
            suffix.append(f"v{version}")
        if document.get("is_current"):
            suffix.append("当前")
        if not suffix:
            return title
        return f"{title} ({', '.join(suffix)})"

    def _normalize_document_match_text(self, value: str) -> str:
        return re.sub(r"[\s\-_()（）\[\]【】,:：.]+", "", str(value or "").strip().lower())

    def _clarification_from_route_decision(self, route_decision: RouteDecision) -> dict:
        reason = route_decision.reason or "当前请求没有明确说明要生成哪种协作产物。"
        return {
            "question": "你希望我接下来怎么整理这段内容？",
            "reason": reason,
            "options": ["整理成需求方案文档", "生成正式答辩 PPT", "画产品流程图", "只做讨论总结"],
            "blocking": True,
        }

    def _protocol_from_route_decision(self, route_decision: RouteDecision) -> RequestProtocol | None:
        if route_decision.route == "status":
            return RequestProtocol(operation="read", object="tasks", route="status")
        if route_decision.route == "tasks":
            return RequestProtocol(operation="analyze", object="tasks", route="tasks")
        if route_decision.route == "summary":
            return RequestProtocol(operation="analyze", object="summary", route="summary")
        if route_decision.route == "risks":
            return RequestProtocol(operation="analyze", object="risks", route="risks")
        if route_decision.route == "doc":
            return RequestProtocol(operation="create", object="doc", route="doc")
        if route_decision.route == "slides":
            return RequestProtocol(operation="create", object="slides", route="slides")
        if route_decision.route == "canvas":
            return RequestProtocol(operation="create", object="canvas", route="canvas")
        if route_decision.route == "help":
            return RequestProtocol(operation="help", object="workspace", route="help")
        if route_decision.route == "unknown" and route_decision.needs_clarification:
            return RequestProtocol(operation="unknown", object="workspace", route="unknown")
        return None

    def _handle_task_status_update_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        return self.task_intent_execution.handle_task_status_update_instruction(
            message,
            route_decision=route_decision,
            active_episode_id=active_episode_id,
            task_run_id=task_run_id,
        )

    def _handle_local_task_assignment_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        return self.task_intent_execution.handle_local_task_assignment_instruction(
            message,
            route_decision=route_decision,
            active_episode_id=active_episode_id,
            task_run_id=task_run_id,
        )

    def _handle_llm_task_intent_instruction(
        self,
        message: FeishuMessageContext,
        *,
        route_decision: RouteDecision,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
    ) -> dict | None:
        return self.task_intent_execution.handle_llm_task_intent_instruction(
            message,
            route_decision=route_decision,
            workspace_context=workspace_context,
            active_episode_id=active_episode_id,
            task_run_id=task_run_id,
        )

    def _sender_actor_names_for_message(self, message: FeishuMessageContext) -> list[str]:
        values: list[str] = []
        for identifier in (
            getattr(message, "sender_id", None),
            getattr(message, "sender_user_id", None),
            getattr(message, "sender_open_id", None),
            getattr(message, "sender_union_id", None),
        ):
            if not identifier:
                continue
            try:
                display_name = self.memory_service.get_alias_display_name(message.session_id, identifier)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load sender alias for task owner matching: session_id=%s error=%s", message.session_id, exc)
                display_name = None
            if display_name and display_name not in values:
                values.append(display_name)
        for identifier in (
            getattr(message, "sender_id", None),
            getattr(message, "sender_user_id", None),
            getattr(message, "sender_open_id", None),
            getattr(message, "sender_union_id", None),
        ):
            if identifier and identifier not in values:
                values.append(identifier)
        return values

    def _primary_sender_actor_name(self, message: FeishuMessageContext) -> str | None:
        names = self._sender_actor_names_for_message(message)
        return names[0] if names else None

    def _team_id_for_message(self, message: FeishuMessageContext) -> str:
        return self.memory_service.normalize_team_id(getattr(message, "tenant_key", None))

    def _join_context_blocks(self, *blocks: str) -> str:
        return "\n\n".join(block.strip() for block in blocks if block and block.strip())

    def _context_tasks_for_message(self, message: FeishuMessageContext) -> list:
        document_tasks, memory_tasks, base_tasks = self._base_status_task_sources_for_message(message)
        pending_tasks = self._pending_discussion_tasks_for_message(message, base_tasks=base_tasks)
        base_tasks = merge_status_task_sources(document_tasks, memory_tasks) if document_tasks else memory_tasks
        merged = merge_status_task_sources(base_tasks, pending_tasks)
        logger.info(
            "Status task context resolved: session_id=%s document_tasks=%s memory_tasks=%s pending_tasks=%s final_tasks=%s source=%s",
            message.session_id,
            len(document_tasks),
            len(memory_tasks),
            len(pending_tasks),
            len(merged),
            "document" if document_tasks else "memory",
        )
        return merged

    def _base_status_task_sources_for_message(self, message: FeishuMessageContext) -> tuple[list[TaskItem], list[TaskItem], list[TaskItem]]:
        document_tasks = self._document_tasks_for_session(message.session_id)
        document_updated_at = self._current_document_updated_at(message.session_id) if document_tasks else None
        memory_tasks: list[TaskItem] = []
        try:
            loaded_memory_tasks = self.memory_service.get_current_tasks(message.session_id)
            memory_tasks = self._filter_memory_tasks_for_status(
                loaded_memory_tasks,
                document_updated_at=document_updated_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current task snapshot for status context: session_id=%s error=%s", message.session_id, exc)
            memory_tasks = []
        if not document_tasks and not memory_tasks and message.chat_type == "p2p":
            try:
                memory_tasks = self.memory_service.get_team_current_tasks(
                    self._team_id_for_message(message),
                    current_session_id=message.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load team task snapshot for status context: session_id=%s error=%s", message.session_id, exc)
                memory_tasks = []
        memory_tasks = merge_status_task_sources([], memory_tasks)
        base_tasks = merge_status_task_sources(document_tasks, memory_tasks) if document_tasks else memory_tasks
        return document_tasks, memory_tasks, base_tasks

    def _current_document_updated_at(self, session_id: str) -> datetime | None:
        try:
            current_doc = self.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document timestamp for status context: session_id=%s error=%s", session_id, exc)
            return None
        if not isinstance(current_doc, dict):
            return None
        if self._document_snapshot_is_source_dirty(current_doc):
            return None
        return self._parse_datetime(current_doc.get("updated_at"))

    def _filter_memory_tasks_for_status(self, tasks: list, *, document_updated_at: datetime | None) -> list:
        if document_updated_at is None:
            return tasks or []
        fresh_tasks = []
        for task in tasks or []:
            task_created_at = self._parse_datetime(getattr(task, "created_at", None))
            if task_created_at is not None and task_created_at > document_updated_at:
                fresh_tasks.append(task)
        return fresh_tasks

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _pending_discussion_tasks_for_message(self, message: FeishuMessageContext, *, base_tasks: list[TaskItem] | None = None) -> list[TaskItem]:
        source_text = self._pending_discussion_text_for_message(message)
        if not source_text:
            return []
        status_update = TaskOperationTool.resolve_status_update(base_tasks or [], source_text)
        if status_update.get("updated"):
            return status_update["tasks"]
        if status_update.get("detected"):
            return []
        if self.llm_service.is_configured():
            try:
                llm_result = self.llm_service.extract_collaboration(source_text)
                llm_tasks = task_items_from_llm_payload(llm_result)
                if llm_tasks:
                    logger.info(
                        "Pending discussion tasks extracted by LLM: session_id=%s tasks=%s",
                        message.session_id,
                        len(llm_tasks),
                    )
                    return normalize_task_dates(normalize_tasks(llm_tasks))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM pending discussion task extraction failed, falling back to local parser: session_id=%s error=%s",
                    message.session_id,
                    exc,
                )
        return normalize_tasks(extract_tasks(source_text))

    def _pending_task_status_update_clarification_for_message(self, message: FeishuMessageContext) -> dict | None:
        source_text = self._pending_discussion_text_for_message(message)
        if not source_text:
            return None
        _, _, base_tasks = self._base_status_task_sources_for_message(message)
        status_update = TaskOperationTool.resolve_status_update(base_tasks, source_text)
        clarification = status_update.get("clarification") if isinstance(status_update, dict) else None
        return clarification if isinstance(clarification, dict) else None

    def _pending_discussion_text_for_message(self, message: FeishuMessageContext) -> str:
        try:
            active_episode = self.memory_service.get_active_episode(message.session_id)
            if active_episode is None:
                return ""
            messages = self.memory_service.get_episode_messages(
                message.session_id,
                episode_id=active_episode.id,
                exclude_message_id=message.message_id,
                limit=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load pending discussion tasks for status context: session_id=%s error=%s", message.session_id, exc)
            return ""
        lines: list[str] = []
        for item in messages:
            content = str(item.content or "").strip()
            if not content:
                continue
            actor_name = self._actor_name_for_sender_id(message.session_id, getattr(item, "sender_id", None))
            normalized_content = self._normalize_pending_discussion_message(item, content)
            lines.append(TaskOperationTool.normalize_first_person_task_text(normalized_content, actor_name))
        return "\n".join(lines)

    def _normalize_pending_discussion_message(self, item: object, content: str) -> str:
        mentioned_names = self._pending_message_mentioned_names(item)
        if not mentioned_names:
            return content
        target_name = mentioned_names[0]
        text = content.strip()
        if re.match(r"^(?:你|请你|麻烦你|需要你|辛苦你|你也|你同时)", text):
            return f"{target_name}{text}"
        return text

    @staticmethod
    def _pending_message_mentioned_names(item: object) -> list[str]:
        raw_mentions = getattr(item, "mentions_json", None)
        if not raw_mentions:
            return []
        try:
            payload = json.loads(raw_mentions) if isinstance(raw_mentions, str) else raw_mentions
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        names: list[str] = []
        for mention in payload:
            if not isinstance(mention, dict) or mention.get("is_bot"):
                continue
            name = str(mention.get("name") or mention.get("display_name") or "").strip()
            fallback_id = str(mention.get("user_id") or mention.get("open_id") or "").strip()
            if name:
                names.append(name)
            elif fallback_id:
                names.append(fallback_id)
        return names

    def _actor_name_for_sender_id(self, session_id: str, sender_id: str | None) -> str | None:
        if not sender_id:
            return None
        try:
            return self.memory_service.get_alias_display_name(session_id, sender_id) or sender_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve actor name for pending discussion: session_id=%s error=%s", session_id, exc)
            return sender_id

    def _document_tasks_for_session(self, session_id: str) -> list[TaskItem]:
        try:
            current_doc = self.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document for status context: session_id=%s error=%s", session_id, exc)
            return []
        if not isinstance(current_doc, dict):
            return []
        if self._document_snapshot_is_source_dirty(current_doc):
            logger.info(
                "Ignoring source-dirty document snapshot for status context: session_id=%s document_id=%s dirty_message_id=%s",
                session_id,
                current_doc.get("document_id"),
                current_doc.get("source_dirty_message_id"),
            )
            return []
        snapshot = current_doc.get("section_snapshot")
        if not isinstance(snapshot, list):
            return []
        return tasks_from_document_snapshot(snapshot)

    @staticmethod
    def _document_snapshot_is_source_dirty(current_doc: dict) -> bool:
        return bool(current_doc.get("source_dirty"))

    def _context_payload_for_message(self, message: FeishuMessageContext) -> dict:
        payload = self.memory_service.load_memory_payload(message.session_id)
        if payload or message.chat_type != "p2p":
            return payload
        return self.memory_service.load_team_memory_payload(
            self._team_id_for_message(message),
            current_session_id=message.session_id,
        )

    def _append_artifacts(self, base: list[dict] | None, *extra: dict | None) -> list[dict]:
        combined = list(base or [])
        for item in extra:
            if item:
                combined.append(item)
        return combined

    def _pause_for_clarification(
        self,
        message: FeishuMessageContext,
        *,
        intent: str,
        clarification: dict,
        active_episode_id: int | None,
        task_run_id: str | None,
        workspace_context: str | None = None,
        artifacts: list[dict] | None = None,
    ) -> dict:
        confirmation_id: str | None = None
        if task_run_id:
            self.task_run_service.update_task_run(
                task_run_id,
                stage="awaiting_user_confirmation",
                status="waiting_confirmation",
            )
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="user_confirmation",
                title="等待用户确认",
                step_type="confirmation",
                status="pending",
                output_payload={
                    "question": clarification["question"],
                    "reason": clarification["reason"],
                    "options": clarification["options"],
                },
            )
            confirmation = self.task_run_service.create_confirmation(
                task_run_id,
                prompt=clarification["question"],
                options=clarification["options"],
            )
            confirmation_id = confirmation.confirmation_id
            self.task_run_service.merge_task_run_metadata(
                task_run_id,
                {
                    "resume_after_confirmation": {
                        "intent": intent,
                        "instruction": message.text,
                        "workspace_context": workspace_context or "",
                        "active_episode_id": active_episode_id,
                        "question": clarification["question"],
                        "reason": clarification["reason"],
                        "options": clarification["options"],
                        "confirmation_id": confirmation_id,
                    }
                },
            )

        reply_preview = self.response_formatter.format_clarification_reply(intent=intent, clarification=clarification)
        result = self.reply_sender.deliver_reply(
            message,
            intent or "help",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=artifacts,
            append_next_actions=False,
            task_run_id=task_run_id,
        )
        if settings.feishu_reply_enabled and settings.feishu_reply_card_enabled:
            try:
                result["reply_card_sent"] = self.reply_sender.send_clarification_card(
                    message,
                    intent=intent or "help",
                    clarification=clarification,
                    task_run_id=task_run_id,
                    confirmation_id=confirmation_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to send Feishu clarification card")
                result["reply_card_error"] = str(exc)
        result["pending_confirmation"] = True
        if confirmation_id:
            result["confirmation_id"] = confirmation_id
        result["response_step_status"] = "pending"
        result["task_run_status"] = "waiting_confirmation"
        result["task_run_stage"] = "awaiting_user_confirmation"
        return result

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str = "user",
    ) -> dict | None:
        if self._should_run_graph_primary():
            graph_result = self.graph_runner.resume_after_confirmation(
                task_run_id,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=answered_by,
            )
            if graph_result is not None:
                return graph_result
        return self.revision_workflow.resume_task_run_after_confirmation(
            task_run_id,
            confirmation_id=confirmation_id,
            answer_value=answer_value,
            answered_by=answered_by,
        )

    def revise_document_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        document_id: str | None = None,
    ):
        return self.revision_workflow.revise_document_from_task_run(
            source_task_run_id,
            instruction=instruction,
            requested_by=requested_by,
            document_id=document_id,
        )

    def bundle_delivery_from_task_run(self, task_run_id: str, *, requested_by: str = "pilot_workbench"):
        return self._delivery_tool().bundle_from_task_run(task_run_id, requested_by=requested_by)

    def _canvas_tool(self) -> CanvasTool:
        self.canvas_tool.artifact_service = self.canvas_artifact_service
        return self.canvas_tool

    def _delivery_tool(self) -> DeliveryTool:
        self.delivery_tool.delivery_artifact_service = self.delivery_artifact_service
        self.delivery_tool.task_run_service = self.task_run_service
        self.delivery_tool.message_api = self.message_api
        return self.delivery_tool

    def _presentation_tool(self) -> PresentationTool:
        self.presentation_tool.artifact_service = self.presentation_artifact_service
        return self.presentation_tool

    def _workbench_revision_tool(self) -> WorkbenchRevisionTool:
        self.workbench_revision_tool.task_run_service = self.task_run_service
        self.workbench_revision_tool.memory_service = self.memory_service
        return self.workbench_revision_tool

    def revise_slides_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        artifact_id: str | None = None,
    ):
        return self.revision_workflow.revise_slides_from_task_run(
            source_task_run_id,
            instruction=instruction,
            requested_by=requested_by,
            artifact_id=artifact_id,
        )


    def _build_analysis_from_llm(
        self,
        *,
        session_id: str,
        source_text: str,
        llm_result: dict,
        intent: str,
        reason: str,
    ) -> AnalyzeResponse:
        llm_tasks = [
            TaskItem.model_validate(item)
            for item in llm_result.get("tasks", [])
            if isinstance(item, dict)
        ]
        current_tasks = self._current_task_items(session_id)
        task_operations = llm_result.get("task_operations", [])

        if isinstance(task_operations, list) and task_operations:
            tasks = TaskOperationTool.apply_llm_operations(current_tasks, task_operations)
        elif current_tasks:
            tasks = TaskOperationTool.update_current_tasks_from_discussion(current_tasks, source_text, llm_tasks)
        elif llm_tasks:
            tasks = TaskOperationTool.merge_task_items(current_tasks, llm_tasks)
        else:
            tasks = apply_discussion_updates(current_tasks, source_text)

        tasks = normalize_task_dates(normalize_tasks(tasks))

        summary = str(llm_result.get("summary") or "").strip() or build_summary(source_text, tasks)
        risks = [str(item).strip() for item in llm_result.get("risks", []) if str(item).strip()]
        if not risks:
            risks = infer_risks(tasks)

        next_actions = [str(item).strip() for item in llm_result.get("next_actions", []) if str(item).strip()]
        if not next_actions:
            next_actions = build_next_actions(tasks, risks)

        traces = [
            AgentTrace(
                agent="router",
                summary=f"LLM reviewed the full workspace context and chose mode={intent}. {reason}".strip(),
            ),
            AgentTrace(
                agent="planner",
                summary=(
                    f"LLM returned {len(tasks)} refreshed task(s)"
                    f" and {len(task_operations) if isinstance(task_operations, list) else 0} task operation(s)"
                    " after considering the whole discussion."
                ),
            ),
            AgentTrace(
                agent="coordinator",
                summary=f"Normalized {len(tasks)} task(s), including date cleanup and field normalization.",
            ),
            AgentTrace(
                agent="reviewer",
                summary=f"Generated {len(risks)} risk signal(s) and {len(next_actions)} next action(s).",
            ),
            AgentTrace(
                agent="memory",
                summary=f"Prepared this round for persistence: {summary}",
            ),
        ]

        return AnalyzeResponse(
            session_id=session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=next_actions[:4],
            agent_traces=traces,
        )

    def _current_task_items(self, session_id: str) -> list[TaskItem]:
        rows = self.memory_service.get_current_tasks(session_id)
        return [
            TaskItem(
                title=row.title,
                owner=row.owner,
                priority=row.priority,
                due_date=row.due_date,
                status=row.status,
                notes=row.notes or "",
            )
            for row in rows
        ]

    def _task_run_title(self, text: str, mode: str | None = None) -> str:
        candidate = " ".join((text or "").split()).strip()
        if candidate:
            candidate = candidate[:40]
        else:
            candidate = "协作运行"
        if mode:
            return f"{self._task_run_title_prefix(mode)} - {candidate}"
        return candidate

    @staticmethod
    def _task_run_title_prefix(mode: str | None) -> str:
        match (mode or "").strip().lower():
            case "doc":
                return "文档产出"
            case "slides":
                return "演示稿产出"
            case "canvas":
                return "画布产出"
            case "delivery":
                return "交付包产出"
            case "tasks":
                return "任务处理"
            case "status":
                return "协作状态"
            case "summary":
                return "讨论总结"
            case "risks":
                return "风险识别"
            case "help":
                return "帮助说明"
            case _:
                return "协作运行"

    @staticmethod
    def _task_run_lifecycle_metadata(mode: str | None, requested_outputs: list[str] | None = None) -> dict[str, str]:
        normalized_mode = (mode or "").strip().lower()
        outputs = [str(item).strip().lower() for item in requested_outputs or [] if str(item).strip()]
        artifact_modes = {"doc", "slides", "canvas", "delivery"}
        if normalized_mode in {"tasks"}:
            run_kind = "task_management"
        elif normalized_mode in {"status", "summary", "risks"}:
            run_kind = "analysis"
        elif normalized_mode == "help":
            run_kind = "help"
        elif normalized_mode in artifact_modes or any(item in artifact_modes for item in outputs):
            run_kind = "artifact_lifecycle"
        else:
            run_kind = "collaboration"

        primary_object = normalized_mode or (outputs[0] if outputs else "workspace")
        if primary_object == "workspace" and outputs:
            primary_object = outputs[0]
        stage_by_object = {
            "doc": "document",
            "slides": "presentation",
            "canvas": "canvas",
            "delivery": "delivery",
            "tasks": "implementation",
            "status": "discussion",
            "summary": "discussion",
            "risks": "discussion",
            "help": "support",
        }
        return {
            "run_kind": run_kind,
            "primary_object": primary_object,
            "lifecycle_stage": stage_by_object.get(primary_object, "discussion"),
        }

    def _condense_text(self, value: str | None) -> str | None:
        candidate = " ".join((value or "").split()).strip()
        return candidate[:200] if candidate else None

    def _extract_first_url(self, lines: list[str]) -> str | None:
        pattern = re.compile(r"https?://\S+")
        for line in lines:
            match = pattern.search(line)
            if match:
                return match.group(0)
        return None

    def _should_close_episode(self, result: dict, reply_preview: str | None) -> bool:
        if not reply_preview:
            return True
        if not settings.feishu_reply_enabled:
            return True
        return bool(result.get("reply_sent"))
