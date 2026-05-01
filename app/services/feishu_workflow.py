import json
import logging
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.doc_api import FeishuDocAPI
from app.feishu.message_api import FeishuMessageAPI
from app.feishu.user_api import FeishuUserAPI
from app.schemas.analyze import AgentTrace, AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.planner import ExecutionPlan, PlannerStep
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.interaction import InteractionService
from app.services.canvas_artifact_service import CanvasArtifactService
from app.services.canvas_tool import CanvasTool
from app.services.delivery_artifact_service import DeliveryArtifactService
from app.services.delivery_tool import DeliveryTool
from app.services.document_package_builder import DocumentPackageBuilder
from app.services.doc_tool import DocTool, DocumentSyncResult
from app.services.execution_planner import ExecutionPlanner, RequestProtocol
from app.services.llm import LLMService
from app.services.memory_service import MemoryService
from app.services.office_artifact_service import OfficeArtifactService
from app.services.presentation_artifact_service import PresentationArtifactService
from app.services.presentation_tool import PresentationTool
from app.services.response_formatter import ResponseFormatter
from app.services.request_router import RequestRouter, RouteDecision
from app.services.session_document_service import SessionDocumentService
from app.services.task_operation_tool import TaskOperationTool
from app.services.task_run_service import TaskRunService
from app.services.workbench_revision_tool import WorkbenchRevisionTool
from app.utils.values import coerce_positive_int
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
        )
        self.workbench_revision_tool = WorkbenchRevisionTool(
            task_run_service=self.task_run_service,
            memory_service=self.memory_service,
        )
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()
        self.execution_planner = ExecutionPlanner()
        self.request_router = RequestRouter()
        self.response_formatter = ResponseFormatter()

    def _doc_tool(self) -> DocTool:
        return DocTool(
            doc_api=self.doc_api,
            session_document_service=self.session_document_service,
        )

    def handle_message(self, message: FeishuMessageContext) -> dict:
        started_at = time.perf_counter()
        active_episode_id: int | None = None
        if message.chat_type == "group":
            try:
                self.memory_service.register_team_group_session(
                    self._team_id_for_message(message),
                    message.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to register group session for team memory: %s", exc)
        if message.chat_type == "group" and not message.is_mentioned:
            active_episode = self.memory_service.ensure_active_episode(message.session_id)
            active_episode_id = active_episode.id

        self._ensure_sender_alias(message)

        saved_content = message.text or message.raw_text
        if not saved_content and message.message_type == "audio":
            saved_content = "[语音消息]"

        self.memory_service.save_user_message(
            session_id=message.session_id,
            message_id=message.message_id,
            sender_id=message.sender_id,
            content=saved_content,
            episode_id=active_episode_id,
            mentioned_users=[user.model_dump() for user in message.mentioned_users],
            embed=False,
        )
        logger.info(
            "Workflow stage completed: message_id=%s stage=save_user_message elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )

        if message.chat_type == "group" and not message.is_mentioned:
            logger.info(
                "Workflow stage completed: message_id=%s stage=buffer_return total_elapsed_ms=%.1f",
                message.message_id,
                (time.perf_counter() - started_at) * 1000,
            )
            return self._empty_result(message.session_id, "buffer")

        task_run = self.task_run_service.create_task_run(
            session_id=message.session_id,
            title=self._task_run_title(message.text),
            source_type=message.chat_type or "unknown",
            source_ref=message.chat_id,
            trigger_message_id=message.message_id,
            created_by=message.sender_id,
            metadata={
                "event_id": message.event_id,
                "chat_id": message.chat_id,
                "is_mentioned": message.is_mentioned,
            },
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="request_received",
            title="接收用户请求",
            step_type="input",
            status="done",
            input_payload={
                "message_id": message.message_id,
                "text": message.text,
                "chat_type": message.chat_type,
            },
        )
        self.task_run_service.update_task_run(task_run.task_run_id, status="running", stage="building_context")

        transcription_notice = getattr(message, "transcription_notice", None)
        if transcription_notice:
            result = self._deliver_reply(
                message,
                "speech_notice",
                transcription_notice,
                analysis=None,
            )
            self.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成处理结果",
                step_type="workflow",
                status="done",
                output_payload={
                    "mode": result["mode"],
                    "reply_preview": result["reply_preview"],
                    "artifact_count": 0,
                },
            )
            self.task_run_service.update_task_run(
                task_run.task_run_id,
                intent=result["mode"],
                title=self._task_run_title(message.text, result["mode"]),
                stage="delivered",
                status="completed",
                latest_summary=self._condense_text(result.get("reply_preview")),
                latest_reply_preview=result.get("reply_preview"),
                latest_error=result.get("reply_error"),
            )
            result["task_run_id"] = task_run.task_run_id
            return result

        try:
            result = self._handle_mentioned_request(message, task_run_id=task_run.task_run_id)
        except Exception as exc:  # noqa: BLE001
            self.task_run_service.update_task_run(
                task_run.task_run_id,
                status="failed",
                stage="failed",
                latest_error=str(exc),
            )
            self.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成处理结果",
                step_type="workflow",
                status="failed",
                error=str(exc),
            )
            raise

        if result["reply_preview"]:
            self.memory_service.save_assistant_message(
                session_id=message.session_id,
                content=result["reply_preview"],
                episode_id=result.get("episode_id"),
                embed=False,
            )
        self._persist_artifacts(task_run.task_run_id, result.get("artifacts", []))
        summary_text = (
            result["analysis"].summary
            if result.get("analysis") is not None
            else self._condense_text(result.get("reply_preview"))
        )
        response_step_status = str(result.get("response_step_status") or "done")
        final_stage = str(result.get("task_run_stage") or "delivered")
        final_status = str(result.get("task_run_status") or "completed")
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="response_generated",
            title="生成处理结果",
            step_type="workflow",
            status=response_step_status,
            output_payload={
                "mode": result["mode"],
                "reply_preview": result["reply_preview"],
                "artifact_count": len(result.get("artifacts", [])),
            },
        )
        if result.get("artifacts"):
            self.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="artifact_persisted",
                title="记录协作产物",
                step_type="artifact",
                status="done",
                output_payload={"artifact_count": len(result["artifacts"])},
            )
        self.task_run_service.update_task_run(
            task_run.task_run_id,
            intent=result["mode"],
            title=self._task_run_title(message.text, result["mode"]),
            stage=final_stage,
            status=final_status,
            latest_summary=summary_text,
            latest_reply_preview=result.get("reply_preview"),
            latest_error=result.get("reply_error"),
        )
        result["task_run_id"] = task_run.task_run_id
        logger.info(
            "Workflow stage completed: message_id=%s stage=workflow_done total_elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return result

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
        active_episode = self.memory_service.get_active_episode(message.session_id)
        active_episode_id = active_episode.id if active_episode else None
        base_workspace_context = self._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        workspace_context = base_workspace_context
        if task_run_id:
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="workspace_context",
                title="构建协作上下文",
                step_type="context",
                status="done",
                output_payload={"context_length": len(base_workspace_context)},
            )

        route_decision = self._route_request(message.text)
        if task_run_id:
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="request_route",
                title="确定请求路由",
                step_type="intent",
                status="done",
                output_payload={
                    "route": route_decision.route,
                    "source": route_decision.source,
                    "confidence": route_decision.confidence,
                    "needs_clarification": route_decision.needs_clarification,
                    "reason": route_decision.reason,
                    "requested_outputs": list(route_decision.requested_outputs),
                },
            )
        logger.info(
            "Request route resolved: message_id=%s route=%s source=%s confidence=%.2f clarification=%s",
            message.message_id,
            route_decision.route,
            route_decision.source,
            route_decision.confidence,
            route_decision.needs_clarification,
        )
        if route_decision.needs_clarification:
            return self._pause_for_clarification(
                message,
                intent=route_decision.route if route_decision.route != "unknown" else "help",
                clarification=self._clarification_from_route_decision(route_decision),
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[],
            )

        target_document = self._resolve_target_document_for_instruction(
            message.session_id,
            message.text,
        ) if route_decision.route == "doc" else None
        document_clarification = self._build_document_selection_clarification(
            message,
            route_decision,
            target_document=target_document,
        )
        if document_clarification:
            return self._pause_for_clarification(
                message,
                intent="doc",
                clarification=document_clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[],
            )

        if self.llm_service.is_configured() and self._should_run_memory_gate(route_decision, message.text):
            try:
                memory_gate = self.llm_service.should_recall_memories(base_workspace_context, message.text)
                if bool(memory_gate.get("should_recall")):
                    workspace_context = self._build_workspace_context_for_message(
                        message,
                        active_episode_id=active_episode_id,
                        include_semantic_search=True,
                    )
                    if task_run_id:
                        self.task_run_service.update_task_run(task_run_id, stage="semantic_recall")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory gate failed, continuing without semantic recall: %s", exc)

        if self.llm_service.is_configured():
            try:
                if task_run_id:
                    self.task_run_service.update_task_run(task_run_id, stage="intent_resolution")
                resolution_context = self._workspace_context_for_route(
                    route_decision,
                    message,
                    workspace_context,
                    active_episode_id=active_episode_id,
                    target_document=target_document,
                )
                llm_result = self._resolve_llm_result_for_route(route_decision, resolution_context, message.text)
                self._apply_route_decision_to_llm_result(llm_result, route_decision)
                protocol = self._normalize_request_protocol(llm_result)
                if task_run_id:
                    self.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_resolution",
                        title="识别任务意图",
                        step_type="intent",
                        status="done",
                        output_payload={
                            "operation": protocol.operation,
                            "object": protocol.object,
                            "route": protocol.route,
                            "route_source": route_decision.source,
                            "route_confidence": route_decision.confidence,
                            "intent": llm_result.get("intent"),
                            "reason": llm_result.get("reason"),
                        },
                    )
                return self._execute_llm_request(
                    message,
                    llm_result,
                    resolution_context,
                    active_episode_id,
                    task_run_id=task_run_id,
                    target_document=target_document,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unified workspace request failed, falling back: %s", exc)
                if task_run_id:
                    self.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_resolution",
                        title="识别任务意图",
                        step_type="intent",
                        status="failed",
                        error=str(exc),
                    )

        if task_run_id:
            self.task_run_service.update_task_run(task_run_id, stage="fallback")
        return self._handle_fallback_request(message, active_episode_id, task_run_id=task_run_id)

    def _build_workspace_context_for_message(
        self,
        message: FeishuMessageContext,
        *,
        active_episode_id: int | None,
        include_semantic_search: bool,
    ) -> str:
        context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            include_semantic_search=include_semantic_search,
            episode_id=active_episode_id,
        )
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
            llm_service=self.llm_service if self.llm_service.is_configured() else None,
        )

    def _apply_route_decision_to_llm_result(self, llm_result: dict, route_decision: RouteDecision) -> None:
        protocol = self._protocol_from_route_decision(route_decision)
        if protocol is None:
            return
        existing_operation = self._normalize_operation(llm_result.get("operation") or llm_result.get("action"))
        existing_object = self._normalize_object(llm_result.get("object") or llm_result.get("target"))
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
        self._store_request_protocol(llm_result, protocol)
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
                        "type": self._required_step_for_protocol(
                            RequestProtocol(operation=operation, object=object_name, route=route)
                        ),
                        "title": self._default_plan_step_title(
                            self._required_step_for_protocol(
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
        if route_decision.route != "doc":
            return workspace_context
        return self._build_doc_update_context(
            message,
            workspace_context,
            active_episode_id=active_episode_id,
            target_document=target_document,
        )

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
                "- 如果讨论中出现新的负责人、截止时间、状态或风险，请同步到对应章节。\n"
                "- 如果没有实质变化，可以返回与当前文档一致的章节内容。"
            )

        latest_block = ""
        if latest_discussion:
            latest_block = latest_discussion.replace("[近期群聊讨论]", "[本次待同步讨论]", 1)

        return self._join_context_blocks(
            workspace_context,
            self._format_current_document_context(current_doc),
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
            "options": ["整理成飞书文档", "生成汇报 PPT 大纲", "只做讨论总结", "整理任务清单"],
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

    def _team_id_for_message(self, message: FeishuMessageContext) -> str:
        return self.memory_service.normalize_team_id(getattr(message, "tenant_key", None))

    def _join_context_blocks(self, *blocks: str) -> str:
        return "\n\n".join(block.strip() for block in blocks if block and block.strip())

    def _format_current_document_context(self, current_doc: dict | None) -> str:
        return DocTool.format_current_document_context(current_doc)

    def _context_tasks_for_message(self, message: FeishuMessageContext) -> list:
        document_tasks = self._document_tasks_for_session(message.session_id)
        document_updated_at = self._current_document_updated_at(message.session_id) if document_tasks else None
        memory_tasks = []
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
        pending_tasks = self._pending_discussion_tasks_for_message(message)
        base_tasks = self._merge_status_task_sources(document_tasks, memory_tasks) if document_tasks else memory_tasks
        merged = self._merge_status_task_sources(base_tasks, pending_tasks)
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

    def _current_document_updated_at(self, session_id: str) -> datetime | None:
        try:
            current_doc = self.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document timestamp for status context: session_id=%s error=%s", session_id, exc)
            return None
        if not isinstance(current_doc, dict):
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

    def _pending_discussion_tasks_for_message(self, message: FeishuMessageContext) -> list[TaskItem]:
        try:
            active_episode = self.memory_service.get_active_episode(message.session_id)
            if active_episode is None:
                return []
            messages = self.memory_service.get_episode_messages(
                message.session_id,
                episode_id=active_episode.id,
                exclude_message_id=message.message_id,
                limit=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load pending discussion tasks for status context: session_id=%s error=%s", message.session_id, exc)
            return []
        source_text = "\n".join(str(item.content or "").strip() for item in messages if str(item.content or "").strip())
        if not source_text:
            return []
        if self.llm_service.is_configured():
            try:
                llm_result = self.llm_service.extract_collaboration(source_text)
                llm_tasks = self._task_items_from_llm_payload(llm_result)
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

    @staticmethod
    def _merge_status_task_sources(primary: list, secondary: list) -> list:
        merged: list[TaskItem] = []
        seen: set[tuple[str, str]] = set()
        for task in [*(primary or []), *(secondary or [])]:
            title = str(getattr(task, "title", "") if not isinstance(task, dict) else task.get("title") or "").strip()
            owner = str(getattr(task, "owner", "") if not isinstance(task, dict) else task.get("owner") or "").strip()
            if not title:
                continue
            key = (title.lower(), owner.lower())
            if key in seen:
                continue
            seen.add(key)
            if isinstance(task, TaskItem):
                merged.append(task)
            elif isinstance(task, dict):
                try:
                    merged.append(TaskItem.model_validate(task))
                except Exception:
                    continue
            else:
                merged.append(
                    TaskItem(
                        title=title,
                        owner=owner or "TBD",
                        priority=str(getattr(task, "priority", "medium") or "medium"),
                        due_date=str(getattr(task, "due_date", "TBD") or "TBD"),
                        status=str(getattr(task, "status", "draft") or "draft"),
                        notes=str(getattr(task, "notes", "") or ""),
                    )
                )
        return merged

    @staticmethod
    def _task_items_from_llm_payload(payload: dict) -> list[TaskItem]:
        raw_tasks = payload.get("tasks") if isinstance(payload, dict) else []
        if not isinstance(raw_tasks, list):
            return []
        tasks: list[TaskItem] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            try:
                tasks.append(TaskItem.model_validate(item))
            except Exception:
                continue
        return tasks

    def _document_tasks_for_session(self, session_id: str) -> list[TaskItem]:
        try:
            current_doc = self.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load current document for status context: session_id=%s error=%s", session_id, exc)
            return []
        if not isinstance(current_doc, dict):
            return []
        snapshot = current_doc.get("section_snapshot")
        if not isinstance(snapshot, list):
            return []
        return self._tasks_from_document_snapshot(snapshot)

    def _tasks_from_document_snapshot(self, snapshot: list[dict]) -> list[TaskItem]:
        tasks: list[TaskItem] = []
        seen: set[tuple[str, str, str]] = set()
        task_heading_markers = ("任务", "待办", "分工", "计划", "事项", "todo", "task")
        for section in snapshot:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            in_task_section = any(marker in heading.lower() for marker in task_heading_markers)
            pending_title = ""
            for paragraph in paragraphs:
                for raw_line in str(paragraph or "").splitlines():
                    line = self._clean_document_task_line(raw_line)
                    if not line:
                        continue
                    item = self._task_item_from_document_line(line, fallback_title=pending_title)
                    if item is None and in_task_section and self._looks_like_task_title(line):
                        pending_title = line
                        continue
                    if item is None:
                        pending_title = ""
                        continue
                    key = (item.title.strip().lower(), item.owner.strip().lower(), item.due_date.strip().lower())
                    if item.title and key not in seen:
                        seen.add(key)
                        tasks.append(item)
                    pending_title = ""
        return tasks

    @staticmethod
    def _clean_document_task_line(value: str) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^\s*[-*]\s+", "", text)
        text = re.sub(r"^\s*\d+[.)、]\s*", "", text)
        text = text.strip()
        if text.startswith("**") and text.endswith("**") and len(text) > 4:
            text = text[2:-2].strip()
        return text

    @staticmethod
    def _looks_like_task_title(line: str) -> bool:
        text = str(line or "").strip()
        if not text or len(text) > 40:
            return False
        if any(marker in text for marker in ("负责人", "截止", "到期", "优先级", "状态", "|")):
            return False
        if text.endswith(("。", "，", "；", ".", ",")):
            return False
        return True

    def _task_item_from_document_line(self, line: str, *, fallback_title: str = "") -> TaskItem | None:
        text = str(line or "").strip()
        if not text:
            return None
        if "|" in text:
            return self._task_item_from_pipe_row(text)
        owner = self._field_value_from_text(text, ("负责人", "owner"))
        due_date = self._field_value_from_text(text, ("截止", "到期", "due"))
        priority = self._field_value_from_text(text, ("优先级", "priority")) or "medium"
        status = self._field_value_from_text(text, ("状态", "status")) or "draft"
        if not owner:
            return None
        title = fallback_title or self._title_before_first_field(text)
        if not title:
            return None
        return TaskItem(title=title, owner=owner, due_date=due_date or "TBD", priority=priority, status=status)

    def _task_item_from_pipe_row(self, line: str) -> TaskItem | None:
        parts = [part.strip().strip("-") for part in str(line or "").split("|") if part.strip()]
        if not parts:
            return None
        content_title = ""
        speaker_note = ""
        for part in parts:
            if self._is_speaker_metadata(part):
                speaker_note = part
                continue
            content_title = self._loose_labeled_value(part, ("内容", "任务内容", "任务描述", "描述", "需求"))
            if content_title:
                break
        first_part_is_metadata = self._is_speaker_metadata(parts[0])
        title = content_title if content_title else self._strip_inline_label(parts[0], ("任务", "事项", "标题"))
        if first_part_is_metadata and not content_title:
            title = ""
        owner = ""
        due_date = "TBD"
        priority = "medium"
        status = "draft"
        notes: list[str] = []
        for index, part in enumerate(parts[1:], start=1):
            labeled_owner = self._field_value_from_text(part, ("负责人", "owner"))
            labeled_due = self._field_value_from_text(part, ("截止", "到期", "due"))
            labeled_priority = self._field_value_from_text(part, ("优先级", "priority"))
            labeled_status = self._field_value_from_text(part, ("状态", "status"))
            if labeled_owner:
                owner = labeled_owner
            elif labeled_due:
                due_date = labeled_due
            elif labeled_priority:
                priority = labeled_priority
            elif labeled_status:
                status = labeled_status
            elif index == 1 and not owner and not any(marker in part for marker in ("截止", "到期", "优先级", "状态")):
                if not self._is_speaker_metadata(part) and not self._loose_labeled_value(
                    part, ("内容", "任务内容", "任务描述", "描述", "需求")
                ):
                    owner = part
            else:
                if not self._loose_labeled_value(part, ("内容", "任务内容", "任务描述", "描述", "需求")):
                    notes.append(part)
        if not title:
            return None
        if speaker_note:
            notes.append(speaker_note)
        return TaskItem(
            title=title,
            owner=owner or "TBD",
            due_date=due_date or "TBD",
            priority=priority or "medium",
            status=status or "draft",
            notes="；".join(notes),
        )

    @staticmethod
    def _is_speaker_metadata(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        return bool(re.match(r"^(发言人|发言者|用户|发送人|sender|speaker)\s*[:：]?\s*\S+", value, flags=re.IGNORECASE))

    @staticmethod
    def _loose_labeled_value(text: str, labels: tuple[str, ...]) -> str:
        value = str(text or "").strip()
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[:：]?\s*(.+)$", value, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _strip_inline_label(text: str, labels: tuple[str, ...]) -> str:
        value = str(text or "").strip()
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[:：]\s*(.+)$", value, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return value

    @staticmethod
    def _field_value_from_text(text: str, labels: tuple[str, ...]) -> str:
        value = str(text or "").strip()
        for label in labels:
            match = re.search(
                rf"{re.escape(label)}\s*[:：]\s*([^|，,；;\n]+)",
                value,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _title_before_first_field(text: str) -> str:
        value = str(text or "").strip()
        match = re.split(r"\s*(?:负责人|owner|截止|到期|due|优先级|priority|状态|status)\s*[:：]", value, maxsplit=1)
        return match[0].strip(" -:：|") if match else ""

    def _context_payload_for_message(self, message: FeishuMessageContext) -> dict:
        payload = self.memory_service.load_memory_payload(message.session_id)
        if payload or message.chat_type != "p2p":
            return payload
        return self.memory_service.load_team_memory_payload(
            self._team_id_for_message(message),
            current_session_id=message.session_id,
        )

    def _normalize_request_protocol(self, llm_result: dict) -> RequestProtocol:
        return self.execution_planner.normalize_request_protocol(llm_result)

    def _store_request_protocol(self, llm_result: dict, protocol: RequestProtocol) -> None:
        self.execution_planner.store_request_protocol(llm_result, protocol)

    def _adjust_protocol_for_instruction(
        self,
        protocol: RequestProtocol,
        *,
        instruction: str,
        llm_result: dict,
    ) -> RequestProtocol:
        return self.execution_planner.adjust_protocol_for_instruction(
            protocol,
            instruction=instruction,
            llm_result=llm_result,
        )

    def _instruction_requests_doc(self, instruction: str) -> bool:
        return self.execution_planner.instruction_requests_doc(instruction)

    def _llm_result_has_doc_package(self, llm_result: dict) -> bool:
        return self.execution_planner.llm_result_has_doc_package(llm_result)

    def _llm_plan_contains_step(self, llm_result: dict, step_type: str) -> bool:
        return self.execution_planner.llm_plan_contains_step(llm_result, step_type)

    def _normalize_token(self, value: object) -> str:
        return self.execution_planner.normalize_token(value)

    def _normalize_operation(self, value: object) -> str:
        return self.execution_planner.normalize_operation(value)

    def _normalize_object(self, value: object) -> str:
        return self.execution_planner.normalize_object(value)

    def _infer_protocol_from_legacy_result(self, llm_result: dict, route: str) -> RequestProtocol:
        return self.execution_planner.infer_protocol_from_legacy_result(llm_result, route)

    def _canonical_route(self, operation: str, object_name: str, fallback: str) -> str:
        return self.execution_planner.canonical_route(operation, object_name, fallback)

    def _allowed_steps_for_protocol(self, protocol: RequestProtocol) -> set[str]:
        return self.execution_planner.allowed_steps_for_protocol(protocol)

    def _required_step_for_protocol(self, protocol: RequestProtocol) -> str:
        return self.execution_planner.required_step_for_protocol(protocol)

    def _enforce_protocol_on_plan(
        self,
        plan: ExecutionPlan,
        *,
        protocol: RequestProtocol,
        reason: str,
        instruction: str,
        llm_result: dict,
    ) -> ExecutionPlan:
        return self.execution_planner.enforce_protocol_on_plan(
            plan,
            protocol=protocol,
            reason=reason,
            instruction=instruction,
            llm_result=llm_result,
        )

    def _execute_llm_request(
        self,
        message: FeishuMessageContext,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> dict:
        protocol = self._normalize_request_protocol(llm_result)
        protocol = self._adjust_protocol_for_instruction(
            protocol,
            instruction=message.text,
            llm_result=llm_result,
        )
        intent = protocol.route
        reason = str(llm_result.get("reason") or "").strip()
        if task_run_id:
            self.task_run_service.update_task_run(
                task_run_id,
                intent=intent or None,
                title=self._task_run_title(message.text, intent or None),
                stage=f"{intent or 'help'}_processing",
            )

        plan = self._resolve_execution_plan(
            intent=intent,
            reason=reason,
            llm_result=llm_result,
            instruction=message.text,
            protocol=protocol,
        )
        logger.info(
            "Execution plan resolved: message_id=%s route=%s operation=%s object=%s steps=%s",
            message.message_id,
            plan.primary_intent,
            llm_result.get("operation"),
            llm_result.get("object"),
            [step.step_type for step in plan.steps],
        )
        plan_artifact = self._build_plan_artifact(plan=plan, reason=reason, llm_result=llm_result)
        if task_run_id:
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="execution_plan",
                title="生成执行计划",
                step_type="plan",
                status="done",
                output_payload={
                    "goal": plan.goal,
                    "primary_intent": plan.primary_intent,
                    "steps": [
                        {
                            "step_id": step.step_id,
                            "step_type": step.step_type,
                            "title": step.title,
                            "depends_on": step.depends_on,
                        }
                        for step in plan.steps
                    ],
                },
            )
        clarification = self._extract_clarification_request(llm_result)
        if clarification and clarification["blocking"]:
            return self._pause_for_clarification(
                message,
                intent=intent,
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[plan_artifact] if plan_artifact else None,
            )

        return self._execute_plan(
            message,
            plan=plan,
            llm_result=llm_result,
            workspace_context=workspace_context,
            active_episode_id=active_episode_id,
            task_run_id=task_run_id,
            base_artifacts=[plan_artifact] if plan_artifact else None,
            target_document=target_document,
        )

    def _append_optional_plan_steps_for_protocol(
        self,
        steps: list[PlannerStep],
        *,
        protocol: RequestProtocol,
        instruction: str,
        llm_result: dict,
    ) -> list[PlannerStep]:
        return self.execution_planner.append_optional_plan_steps_for_protocol(
            steps,
            protocol=protocol,
            instruction=instruction,
            llm_result=llm_result,
        )

    def _extract_clarification_request(self, llm_result: dict) -> dict | None:
        return self.execution_planner.extract_clarification_request(llm_result)

    def _resolve_execution_plan(
        self,
        *,
        intent: str,
        reason: str,
        llm_result: dict,
        instruction: str,
        protocol: RequestProtocol | None = None,
    ) -> ExecutionPlan:
        return self.execution_planner.resolve_execution_plan(
            intent=intent,
            reason=reason,
            llm_result=llm_result,
            instruction=instruction,
            protocol=protocol,
        )

    def _normalize_execution_plan(
        self,
        raw_plan: dict,
        *,
        intent: str,
        instruction: str,
    ) -> ExecutionPlan:
        return self.execution_planner.normalize_execution_plan(
            raw_plan,
            intent=intent,
            instruction=instruction,
        )

    def _build_fallback_plan(
        self,
        *,
        intent: str,
        reason: str,
        instruction: str,
        llm_result: dict,
    ) -> ExecutionPlan:
        return self.execution_planner.build_fallback_plan(
            intent=intent,
            reason=reason,
            instruction=instruction,
            llm_result=llm_result,
        )

    def _normalize_plan_step_type(self, raw_type: str) -> str:
        return self.execution_planner.normalize_plan_step_type(raw_type)

    def _default_plan_goal(self, intent: str, instruction: str, *, reason: str | None = None) -> str:
        return self.execution_planner.default_plan_goal(intent, instruction, reason=reason)

    def _default_plan_step_title(self, step_type: str, intent: str) -> str:
        return self.execution_planner.default_plan_step_title(step_type, intent)

    def _should_include_slides_step(self, intent: str, instruction: str, llm_result: dict) -> bool:
        return self.execution_planner.should_include_slides_step(intent, instruction, llm_result)

    def _build_plan_artifact(self, *, plan: ExecutionPlan, reason: str, llm_result: dict) -> dict | None:
        return self.execution_planner.build_plan_artifact(plan=plan, reason=reason, llm_result=llm_result)

    def _append_artifacts(self, base: list[dict] | None, *extra: dict | None) -> list[dict]:
        combined = list(base or [])
        for item in extra:
            if item:
                combined.append(item)
        return combined

    def _execute_plan(
        self,
        message: FeishuMessageContext,
        *,
        plan: ExecutionPlan,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
        base_artifacts: list[dict] | None = None,
        target_document: dict | None = None,
    ) -> dict:
        combined_artifacts = list(base_artifacts or [])
        reply_parts: list[str] = []
        final_analysis: AnalyzeResponse | None = None
        close_title: str | None = None

        if not plan.steps:
            fallback_reply = self._format_help_reply("当前还没有可执行的计划步骤。")
            return self._deliver_reply(
                message,
                plan.primary_intent or "help",
                fallback_reply,
                analysis=None,
                episode_id=active_episode_id,
                artifacts=combined_artifacts,
            )

        for index, step in enumerate(plan.steps, start=1):
            if task_run_id:
                self.task_run_service.update_task_run(
                    task_run_id,
                    stage=f"executing_{step.step_type}",
                    status="running",
                )
                self.task_run_service.upsert_step(
                    task_run_id,
                    step_key=f"plan_{index}_{step.step_type}",
                    title=step.title,
                    step_type="plan_execution",
                    status="running",
                    output_payload={
                        "step_id": step.step_id,
                        "step_type": step.step_type,
                        "depends_on": step.depends_on,
                        "notes": step.notes,
                    },
                )

            try:
                step_result = self._execute_plan_step(
                    message,
                    step=step,
                    plan=plan,
                    llm_result=llm_result,
                    workspace_context=workspace_context,
                    active_episode_id=active_episode_id,
                    task_run_id=task_run_id,
                    target_document=target_document,
                )
            except Exception as exc:  # noqa: BLE001
                if task_run_id:
                    self.task_run_service.upsert_step(
                        task_run_id,
                        step_key=f"plan_{index}_{step.step_type}",
                        title=step.title,
                        step_type="plan_execution",
                        status="failed",
                        error=str(exc),
                    )
                raise

            if task_run_id:
                self.task_run_service.upsert_step(
                    task_run_id,
                    step_key=f"plan_{index}_{step.step_type}",
                    title=step.title,
                    step_type="plan_execution",
                    status="done",
                    output_payload={
                        "step_id": step.step_id,
                        "step_type": step.step_type,
                        "reply_preview": step_result.get("reply_preview"),
                        "artifact_count": len(step_result.get("artifacts", [])),
                    },
                )

            reply_preview = str(step_result.get("reply_preview") or "").strip()
            if reply_preview:
                reply_parts.append(reply_preview)
            combined_artifacts.extend(step_result.get("artifacts", []))
            if step_result.get("analysis") is not None:
                final_analysis = step_result["analysis"]
            if step_result.get("close_title"):
                close_title = str(step_result["close_title"])

        final_reply = self._combine_plan_replies(reply_parts)
        result = self._deliver_reply(
            message,
            plan.primary_intent or "help",
            final_reply,
            analysis=final_analysis,
            episode_id=active_episode_id,
            artifacts=combined_artifacts,
        )
        if active_episode_id is not None and self._should_close_episode(result, final_reply):
            self.memory_service.close_active_episode(
                message.session_id,
                title=close_title or (final_analysis.summary if final_analysis is not None else plan.primary_intent),
            )
        return result

    def _execute_plan_step(
        self,
        message: FeishuMessageContext,
        *,
        step: PlannerStep,
        plan: ExecutionPlan,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
        target_document: dict | None = None,
    ) -> dict:
        if step.step_type == "generate_slides":
            return self._prepare_slides_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=task_run_id,
            )
        if step.step_type == "generate_canvas":
            return self._prepare_canvas_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=task_run_id,
            )
        if step.step_type == "sync_doc":
            return self._prepare_doc_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=active_episode_id,
                reason=plan.goal,
                task_run_id=task_run_id,
                target_document=target_document,
            )
        if step.step_type == "answer_status":
            return self._prepare_status_execution(
                message,
                llm_result=llm_result,
                active_episode_id=active_episode_id,
            )
        if step.step_type == "analyze_discussion":
            return self._prepare_analysis_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=active_episode_id,
                intent=plan.primary_intent,
            )
        return self._prepare_help_execution(reason=str(llm_result.get("reason") or "").strip())

    def _prepare_slides_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None = None,
    ) -> dict:
        package = llm_result.get("slides")
        provider = "llm"
        if not isinstance(package, dict) or not package.get("slides"):
            try:
                package = self.llm_service.generate_presentation_package(workspace_context, message.text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slide package fallback generation failed: %s", exc)
                package = self._build_fallback_presentation_package(message.session_id)
                provider = "fallback"
        presentation_tool = self._presentation_tool()
        reply_preview = presentation_tool.format_reply(package)
        artifact = presentation_tool.persist_artifact(
            package,
            provider=provider,
            session_id=message.session_id,
            task_run_id=task_run_id,
        )
        return {
            "reply_preview": reply_preview,
            "analysis": None,
            "artifacts": [artifact],
            "close_title": "slides",
        }

    def _prepare_canvas_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None = None,
    ) -> dict:
        canvas = llm_result.get("canvas") if isinstance(llm_result.get("canvas"), dict) else {}
        title = str(canvas.get("title") or self._task_run_title(message.text, "canvas")).strip() or "Canvas"
        canvas_tool = self._canvas_tool()
        artifact = canvas_tool.generate_flow_artifact(
            title=title,
            instruction=message.text,
            llm_result=llm_result,
            workspace_context=workspace_context,
            task_run_id=task_run_id,
            session_id=message.session_id,
        )
        reply_preview = canvas_tool.format_reply(artifact)
        return {
            "reply_preview": reply_preview,
            "analysis": None,
            "artifacts": [artifact],
            "close_title": str(artifact.get("title") or "canvas"),
        }

    def _prepare_doc_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        reason: str,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> dict:
        package, analysis = self._build_doc_response_package(
            session_id=message.session_id,
            instruction=message.text,
            llm_result=llm_result,
            workspace_context=workspace_context,
            episode_id=active_episode_id,
            reason=reason,
            source_message_id=message.message_id,
        )
        package["title"] = self._compose_doc_title(
            str(package.get("title") or "协同文档"),
            stats_as_of=str(package.get("stats_as_of") or "").strip() or None,
        )
        edit_plan = package.get("artifact_edit_plan") if isinstance(package.get("artifact_edit_plan"), dict) else {}
        logger.info(
            "Starting document sync: message_id=%s session_id=%s sections=%s has_edit_plan=%s target_document=%s",
            message.message_id,
            message.session_id,
            len(package.get("sections", [])) if isinstance(package.get("sections"), list) else 0,
            bool(edit_plan),
            bool(target_document),
        )
        sync_result = self._sync_package_to_session_doc(
            package,
            session_id=message.session_id,
            episode_id=active_episode_id,
            instruction=message.text,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        artifact = self._build_document_artifact(
            session_id=message.session_id,
            package=package,
            sync_result=sync_result,
            fallback_provider="local",
            task_run_id=task_run_id,
        )
        sync_lines = sync_result.summary_lines
        reply_preview = self._format_doc_reply(package, sync_lines)
        return {
            "reply_preview": reply_preview,
            "analysis": analysis,
            "artifacts": [artifact],
            "close_title": str(package.get("title") or "doc"),
        }

    def _prepare_status_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        active_episode_id: int | None = None,
    ) -> dict:
        tasks = self._context_tasks_for_message(message)
        payload = self._context_payload_for_message(message)
        status_answer = self._format_status_reply(message.text, tasks, payload)
        if active_episode_id is not None and tasks:
            self._persist_status_task_snapshot(
                message.session_id,
                tasks,
                episode_id=active_episode_id,
            )
        return {
            "reply_preview": status_answer,
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }

    def _persist_status_task_snapshot(
        self,
        session_id: str,
        tasks: list,
        *,
        episode_id: int | None,
    ) -> None:
        normalized_tasks: list[TaskItem] = []
        for task in tasks:
            if isinstance(task, TaskItem):
                normalized_tasks.append(task)
                continue
            try:
                normalized_tasks.append(TaskItem.model_validate(task))
            except Exception:
                continue
        if not normalized_tasks:
            return
        risks = infer_risks(normalized_tasks)
        analysis = AnalyzeResponse(
            session_id=session_id,
            summary="根据当前文档和最新讨论刷新任务快照。",
            tasks=normalized_tasks,
            risks=risks,
            next_actions=build_next_actions(normalized_tasks, risks),
            agent_traces=[AgentTrace(agent="status", summary="persisted task snapshot from status query")],
        )
        try:
            self.memory_service.save_round(
                session_id=session_id,
                analysis=analysis,
                episode_id=episode_id,
                embed=False,
                async_embed=False,
                preserve_unmatched_previous=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist status task snapshot: session_id=%s error=%s", session_id, exc)

    def _prepare_analysis_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        intent: str,
    ) -> dict:
        source_text = self.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        ) or workspace_context or message.text
        analysis = self._build_analysis_from_llm(
            session_id=message.session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent=intent,
            reason=str(llm_result.get("reason") or "").strip(),
        )
        reply_preview = self._format_analysis_reply(analysis, intent)
        self.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        return {
            "reply_preview": reply_preview,
            "analysis": analysis,
            "artifacts": [],
            "close_title": analysis.summary,
        }

    def _prepare_help_execution(self, *, reason: str) -> dict:
        return {
            "reply_preview": self._format_help_reply(reason),
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }

    def _combine_plan_replies(self, reply_parts: list[str]) -> str | None:
        return self.response_formatter.combine_plan_replies(reply_parts)

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

        reply_preview = self._format_clarification_reply(intent=intent, clarification=clarification)
        result = self._deliver_reply(
            message,
            intent or "help",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=artifacts,
        )
        result["pending_confirmation"] = True
        if confirmation_id:
            result["confirmation_id"] = confirmation_id
        result["response_step_status"] = "pending"
        result["task_run_status"] = "waiting_confirmation"
        result["task_run_stage"] = "awaiting_user_confirmation"
        return result

    def _format_clarification_reply(self, *, intent: str, clarification: dict) -> str:
        return self.response_formatter.format_clarification_reply(intent=intent, clarification=clarification)

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str = "user",
    ) -> dict | None:
        detail = self.task_run_service.get_task_run(task_run_id)
        if detail is None:
            return None

        metadata = self.task_run_service.get_task_run_metadata(task_run_id)
        resume_payload = metadata.get("resume_after_confirmation")
        if not isinstance(resume_payload, dict):
            return None

        expected_confirmation_id = str(resume_payload.get("confirmation_id") or "").strip()
        if expected_confirmation_id and expected_confirmation_id != confirmation_id:
            return None

        instruction = str(resume_payload.get("instruction") or "").strip()
        workspace_context = str(resume_payload.get("workspace_context") or "")
        active_episode_id = resume_payload.get("active_episode_id")
        if not isinstance(active_episode_id, int):
            active_episode_id = None

        resumed_instruction = self._build_confirmation_resume_instruction(instruction, answer_value)
        intent = str(resume_payload.get("intent") or "status")
        target_document = None
        if intent == "doc":
            target_document = self._resolve_target_document_for_instruction(detail.session_id, resumed_instruction)
            if target_document is not None:
                workspace_context = self._join_context_blocks(
                    workspace_context,
                    self._format_current_document_context(target_document),
                )
        self.task_run_service.upsert_step(
            task_run_id,
            step_key="user_confirmation",
            title="等待用户确认",
            step_type="confirmation",
            status="done",
            output_payload={
                "question": resume_payload.get("question"),
                "reason": resume_payload.get("reason"),
                "options": resume_payload.get("options"),
                "answer_value": answer_value,
                "answered_by": answered_by,
            },
        )
        self.task_run_service.update_task_run(task_run_id, stage="confirmation_replanning", status="running")

        metadata.pop("resume_after_confirmation", None)
        metadata["last_confirmation"] = {
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
        }
        self.task_run_service.update_task_run(task_run_id, metadata=metadata)

        if not self.llm_service.is_configured():
            result = {
                "session_id": detail.session_id,
                "episode_id": active_episode_id,
                "mode": intent,
                "analysis": None,
                "reply_preview": f"已记录确认：{answer_value}。当前未配置可继续自动执行的 LLM，请稍后重新发起一次请求。",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            }
            self._persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
            return result

        route_decision = RouteDecision(route=intent, source="confirmation", confidence=1.0)
        llm_result = self._resolve_llm_result_for_route(route_decision, workspace_context, resumed_instruction)
        self._apply_route_decision_to_llm_result(llm_result, route_decision)
        synthetic_message = SimpleNamespace(
            session_id=detail.session_id,
            message_id=detail.trigger_message_id or confirmation_id,
            text=resumed_instruction,
            chat_id=None,
        )
        result = self._execute_llm_request(
            synthetic_message,
            llm_result,
            workspace_context,
            active_episode_id,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        self._persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
        return result

    def revise_document_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        document_id: str | None = None,
    ):
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Document revision instruction cannot be empty.")

        source_detail = self.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        session_id = source_detail.session_id
        revision_start = self._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=self._task_run_title(cleaned_instruction, "doc_revision"),
            intent="doc",
            requested_by=requested_by,
            metadata={
                "source_task_run_id": source_task_run_id,
                "revision_instruction": cleaned_instruction,
                "requested_by": requested_by,
                "document_id": (document_id or "").strip() or None,
            },
            step_title="接收文档修订指令",
            input_payload={
                "source_task_run_id": source_task_run_id,
                "instruction": cleaned_instruction,
                "requested_by": requested_by,
                "document_id": (document_id or "").strip() or None,
            },
            stage="building_context",
        )
        task_run = revision_start.task_run

        synthetic_message_id = revision_start.synthetic_message_id
        self._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="document",
        )
        workspace_context = self._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="document",
        )
        try:
            requested_document_id = (document_id or "").strip()
            if requested_document_id:
                current_doc = self.session_document_service.get_document(session_id, requested_document_id)
                if current_doc is None:
                    raise ValueError(f"Document not found in session history: {requested_document_id}")
            else:
                current_doc = self.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ValueError):
                raise
            logger.warning("Failed to load current document context for revision: %s", exc)
            current_doc = None
        workspace_context = self._join_context_blocks(
            workspace_context,
            self._format_current_document_context(current_doc),
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建文档修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        llm_result: dict = {}
        if self.llm_service.is_configured():
            try:
                self.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_planning")
                revision_instruction = DocTool.build_document_revision_instruction(
                    cleaned_instruction,
                    current_doc=current_doc,
                )
                try:
                    llm_result = self.llm_service.resolve_doc_request(workspace_context, revision_instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Specialized doc revision request failed, falling back to workspace resolver: %s",
                        exc,
                    )
                    llm_result = self.llm_service.resolve_workspace_request(
                        workspace_context,
                        revision_instruction,
                    )
                llm_result["operation"] = "update"
                llm_result["object"] = "doc"
                llm_result["route"] = "doc"
                llm_result["intent"] = "doc"
                self.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="识别文档修订目标",
                    step_type="intent",
                    status="done",
                    output_payload={
                        "intent": llm_result.get("intent"),
                        "reason": llm_result.get("reason"),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Document revision planning failed, falling back to deterministic doc sync: %s", exc)
                self.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="识别文档修订目标",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )

        self.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_processing")
        synthetic_message = SimpleNamespace(
            session_id=session_id,
            message_id=synthetic_message_id,
            text=cleaned_instruction,
            chat_id=None,
        )
        try:
            result = self._prepare_doc_execution(
                synthetic_message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=None,
                reason="根据工作台指令修订当前协作文档",
                task_run_id=task_run.task_run_id,
                target_document=current_doc,
            )
            result["mode"] = "doc"
            result["session_id"] = session_id
            result["episode_id"] = None
            result["reply_sent"] = False
            result["reply_error"] = None
            self._persist_task_run_result(
                task_run.task_run_id,
                message_text=cleaned_instruction,
                result=result,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.task_run_service.update_task_run(
                task_run.task_run_id,
                stage="failed",
                status="failed",
                latest_error=str(exc),
            )
            self.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成文档修订结果",
                step_type="workflow",
                status="failed",
                error=str(exc),
            )
            raise

        return self.task_run_service.get_task_run(task_run.task_run_id)

    def bundle_delivery_from_task_run(self, task_run_id: str, *, requested_by: str = "pilot_workbench"):
        return self._delivery_tool().bundle_from_task_run(task_run_id, requested_by=requested_by)

    def _canvas_tool(self) -> CanvasTool:
        self.canvas_tool.artifact_service = self.canvas_artifact_service
        return self.canvas_tool

    def _delivery_tool(self) -> DeliveryTool:
        self.delivery_tool.delivery_artifact_service = self.delivery_artifact_service
        self.delivery_tool.task_run_service = self.task_run_service
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
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Slides revision instruction cannot be empty.")

        source_detail = self.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        presentation_tool = self._presentation_tool()
        source_artifact = presentation_tool.resolve_slides_artifact(
            getattr(source_detail, "artifacts", []),
            artifact_id=artifact_id,
        )
        if source_artifact is None:
            raise ValueError("No slides package artifact found for this task run.")
        source_artifact_id = presentation_tool.artifact_field(source_artifact, "artifact_id")
        current_package = presentation_tool.preview_payload(
            presentation_tool.artifact_field(source_artifact, "preview_json")
        )
        if not current_package:
            raise ValueError("Slides package artifact has no structured preview to revise.")

        session_id = source_detail.session_id
        revision_start = self._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=self._task_run_title(cleaned_instruction, "slides_revision"),
            intent="slides",
            requested_by=requested_by,
            metadata={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "revision_instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            step_title="接收演示稿修订指令",
            input_payload={
                "source_task_run_id": source_task_run_id,
                "source_artifact_id": source_artifact_id,
                "instruction": cleaned_instruction,
                "requested_by": requested_by,
            },
            stage="slides_revision_context",
        )
        task_run = revision_start.task_run

        synthetic_message_id = revision_start.synthetic_message_id
        self._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = self._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = self._join_context_blocks(
            workspace_context,
            "[当前演示稿包]\n" + json.dumps(current_package, ensure_ascii=False)[:12000],
        )
        self.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建演示稿修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        provider = "local"
        revised_package: dict
        edit_plan = presentation_tool.plan_revision(current_package, cleaned_instruction)
        self.task_run_service.update_task_run(task_run.task_run_id, stage="slides_revision_processing")
        if self.llm_service.is_configured():
            try:
                revised_package = self.llm_service.revise_presentation_package(
                    current_package,
                    workspace_context,
                    cleaned_instruction,
                )
                edit_plan = presentation_tool.plan_revision(
                    current_package,
                    cleaned_instruction,
                    revised_package,
                )
                if edit_plan.mutation_required and not presentation_tool.package_changed(current_package, revised_package):
                    revised_package = presentation_tool.revise_deterministic(
                        current_package,
                        cleaned_instruction,
                        edit_plan=edit_plan,
                    )
                provider = "llm"
                self.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="生成演示稿修订方案",
                    step_type="intent",
                    status="done",
                    output_payload={"provider": provider},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM slides revision failed, using deterministic fallback: %s", exc)
                revised_package = presentation_tool.revise_deterministic(
                    current_package,
                    cleaned_instruction,
                    edit_plan=edit_plan,
                )
                provider = "fallback"
                self.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="生成演示稿修订方案",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )
        else:
            revised_package = presentation_tool.revise_deterministic(
                current_package,
                cleaned_instruction,
                edit_plan=edit_plan,
            )

        if edit_plan.mutation_required and not presentation_tool.package_changed(current_package, revised_package):
            error = "Slides revision produced no visible artifact changes; please clarify the target page or edit."
            self.task_run_service.update_task_run(task_run.task_run_id, status="failed", error=error)
            raise ValueError(error)

        base_version = coerce_positive_int(
            current_package.get("version") or presentation_tool.artifact_field(source_artifact, "version")
        )
        revised_package["version"] = max(base_version + 1, 2)
        artifact = presentation_tool.persist_artifact(
            revised_package,
            provider=provider,
            session_id=session_id,
            task_run_id=task_run.task_run_id,
        )
        reply_preview = "【演示稿修订】\n" + presentation_tool.format_reply(revised_package)
        result = {
            "session_id": session_id,
            "episode_id": None,
            "mode": "slides",
            "analysis": None,
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "artifacts": [artifact],
        }
        self._persist_task_run_result(
            task_run.task_run_id,
            message_text=cleaned_instruction,
            result=result,
            session_id=session_id,
        )
        return self.task_run_service.get_task_run(task_run.task_run_id)

    def _build_confirmation_resume_instruction(self, instruction: str, answer_value: str) -> str:
        base = instruction.strip() or "继续刚才的任务"
        return f"{base}\n\n[用户刚刚确认]\n{answer_value}"

    def _persist_task_run_result(self, task_run_id: str, *, message_text: str, result: dict, session_id: str) -> None:
        if result["reply_preview"]:
            self.memory_service.save_assistant_message(
                session_id=session_id,
                content=result["reply_preview"],
                episode_id=result.get("episode_id"),
                embed=False,
            )
        self._persist_artifacts(task_run_id, result.get("artifacts", []))
        summary_text = (
            result["analysis"].summary
            if result.get("analysis") is not None
            else self._condense_text(result.get("reply_preview"))
        )
        response_step_status = str(result.get("response_step_status") or "done")
        final_stage = str(result.get("task_run_stage") or "delivered")
        final_status = str(result.get("task_run_status") or "completed")
        self.task_run_service.upsert_step(
            task_run_id,
            step_key="response_generated",
            title="生成处理结果",
            step_type="workflow",
            status=response_step_status,
            output_payload={
                "mode": result["mode"],
                "reply_preview": result["reply_preview"],
                "artifact_count": len(result.get("artifacts", [])),
            },
        )
        if result.get("artifacts"):
            self.task_run_service.upsert_step(
                task_run_id,
                step_key="artifact_persisted",
                title="记录协作产物",
                step_type="artifact",
                status="done",
                output_payload={"artifact_count": len(result["artifacts"])},
            )
        self.task_run_service.update_task_run(
            task_run_id,
            intent=result["mode"],
            title=self._task_run_title(message_text, result["mode"]),
            stage=final_stage,
            status=final_status,
            latest_summary=summary_text,
            latest_reply_preview=result.get("reply_preview"),
            latest_error=result.get("reply_error"),
        )

    def _persist_artifacts(self, task_run_id: str, artifacts: list | None) -> None:
        for artifact in artifacts or []:
            if not isinstance(artifact, dict):
                continue
            self.task_run_service.create_artifact(
                task_run_id,
                artifact_type=str(artifact.get("artifact_type") or "note"),
                title=str(artifact.get("title") or "协作产物"),
                provider=str(artifact.get("provider") or "local"),
                status=str(artifact.get("status") or "ready"),
                url=str(artifact.get("url") or "").strip() or None,
                preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
                version=coerce_positive_int(artifact.get("version")),
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
            tasks = self._apply_llm_task_operations(current_tasks, task_operations)
        elif current_tasks:
            tasks = self._update_current_tasks_from_discussion(current_tasks, source_text, llm_tasks)
        elif llm_tasks:
            tasks = self._merge_task_items(current_tasks, llm_tasks)
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

    def _apply_llm_task_operations(self, current_tasks: list[TaskItem], operations: list[dict]) -> list[TaskItem]:
        return TaskOperationTool.apply_llm_operations(current_tasks, operations)

    def _merge_task_items(self, current_tasks: list[TaskItem], refreshed_tasks: list[TaskItem]) -> list[TaskItem]:
        return TaskOperationTool.merge_task_items(current_tasks, refreshed_tasks)

    def _update_current_tasks_from_discussion(
        self,
        current_tasks: list[TaskItem],
        source_text: str,
        llm_tasks: list[TaskItem],
    ) -> list[TaskItem]:
        return TaskOperationTool.update_current_tasks_from_discussion(
            current_tasks,
            source_text,
            llm_tasks,
        )

    def _find_merge_target(self, tasks: list[TaskItem], incoming_task: TaskItem) -> int | None:
        return TaskOperationTool.find_merge_target(tasks, incoming_task)

    def _find_operation_target(
        self,
        tasks: list[TaskItem],
        match_hint: dict,
        task_payload: dict | None,
    ) -> int | None:
        return TaskOperationTool.find_operation_target(tasks, match_hint, task_payload)

    def _handle_fallback_request(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        decision = self.interaction_service.decide(message.text)
        if task_run_id:
            self.task_run_service.update_task_run(
                task_run_id,
                intent=decision.mode,
                title=self._task_run_title(message.text, decision.mode),
                stage=f"{decision.mode}_fallback",
            )

        if decision.mode == "help":
            return self._deliver_reply(message, "help", self._format_help_reply(), analysis=None)

        if decision.mode == "slides":
            return self._handle_fallback_slides(message, active_episode_id, task_run_id=task_run_id)
        if decision.mode == "doc":
            return self._handle_fallback_doc(message, active_episode_id, task_run_id=task_run_id)
        if decision.mode == "canvas":
            return self._handle_fallback_canvas(message, active_episode_id, task_run_id=task_run_id)

        if decision.mode == "status":
            tasks = self._context_tasks_for_message(message)
            payload = self._context_payload_for_message(message)
            if active_episode_id is not None and tasks:
                self._persist_status_task_snapshot(
                    message.session_id,
                    tasks,
                    episode_id=active_episode_id,
                )
            reply_preview = self._format_status_reply(message.text, tasks, payload)
            return self._deliver_reply(message, "status", reply_preview, analysis=None, episode_id=active_episode_id)

        discussion_block = self.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        )
        if not discussion_block:
            reply = "我已经开始旁听这轮讨论了。你继续聊，等需要的时候再 @我做总结、整理待办或生成汇报大纲。"
            return self._deliver_reply(message, "help", reply, analysis=None)

        analysis = self.orchestrator.run(
            AnalyzeRequest(session_id=message.session_id, raw_text=discussion_block)
        )
        reply_preview = self._format_analysis_reply(analysis, decision.mode)
        self.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            async_embed=True,
        )
        result = self._deliver_reply(message, decision.mode, reply_preview, analysis=analysis, episode_id=active_episode_id)
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title=analysis.summary)
        return result

    def _handle_fallback_slides(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workspace_context = self._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        if not workspace_context.strip():
            reply = "我这边还没有拿到可用的讨论素材。先在群里把目标、分工和结论聊出来，再让我生成汇报大纲会更准确。"
            return self._deliver_reply(message, "slides", reply, analysis=None)

        provider = "llm"
        try:
            package = self.llm_service.generate_presentation_package(workspace_context, message.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            package = self._build_fallback_presentation_package(message.session_id)
            provider = "fallback"

        presentation_tool = self._presentation_tool()
        reply_preview = presentation_tool.format_reply(package)
        artifact = presentation_tool.persist_artifact(
            package,
            provider=provider,
            session_id=message.session_id,
            task_run_id=task_run_id,
        )
        result = self._deliver_reply(
            message,
            "slides",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=[artifact],
        )
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title="slides")
        return result

    def _handle_fallback_canvas(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        try:
            workspace_context = self._build_workspace_context_for_message(
                message,
                active_episode_id=active_episode_id,
                include_semantic_search=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Canvas fallback context loading failed, using instruction only: %s", exc)
            workspace_context = ""
        step_result = self._prepare_canvas_execution(
            message,
            llm_result={},
            workspace_context=workspace_context,
            task_run_id=task_run_id,
        )
        result = self._deliver_reply(
            message,
            "canvas",
            step_result.get("reply_preview"),
            analysis=None,
            episode_id=active_episode_id,
            artifacts=step_result.get("artifacts", []),
        )
        if active_episode_id is not None and self._should_close_episode(result, str(step_result.get("reply_preview") or "")):
            self.memory_service.close_active_episode(message.session_id, title=str(step_result.get("close_title") or "canvas"))
        return result

    def _handle_fallback_doc(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workspace_context = self._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        package, analysis = self._build_doc_response_package(
            session_id=message.session_id,
            instruction=message.text,
            llm_result={},
            workspace_context=workspace_context,
            episode_id=active_episode_id,
            reason="为文档同步生成结构化沉淀",
            source_message_id=message.message_id,
        )
        sync_result = self._sync_package_to_session_doc(
            package,
            session_id=message.session_id,
            episode_id=active_episode_id,
            instruction=message.text,
            task_run_id=task_run_id,
        )
        artifact = self._build_document_artifact(
            session_id=message.session_id,
            package=package,
            sync_result=sync_result,
            fallback_provider="fallback",
            task_run_id=task_run_id,
        )
        sync_lines = sync_result.summary_lines
        reply_preview = self._format_doc_reply(package, sync_lines)
        result = self._deliver_reply(
            message,
            "doc",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
            artifacts=[artifact],
        )
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title=str(package.get("title") or "doc"))
        return result

    def _build_document_package_from_workspace(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        episode_id: int | None,
    ) -> dict:
        stats_as_of = self._resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        provided_package = self._document_package_from_llm_result(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            episode_id=episode_id,
            stats_as_of=stats_as_of,
        )
        if provided_package:
            return provided_package

        if self._should_include_slides_step("doc", instruction, llm_result):
            slides = llm_result.get("slides")
            if not isinstance(slides, dict) or not slides.get("slides"):
                try:
                    slides = self.llm_service.generate_presentation_package(workspace_context, instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Document fallback slide generation failed: %s", exc)
                    slides = self._build_fallback_presentation_package(session_id)
            return self._document_from_presentation(slides, instruction, stats_as_of=stats_as_of)

        source_text = self.memory_service.build_discussion_block(
            session_id,
            episode_id=episode_id,
        ) or workspace_context or instruction
        analysis = self._build_analysis_from_llm(
            session_id=session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent="summary",
            reason="为文档同步生成结构化沉淀",
        )
        return self._document_from_analysis(analysis, instruction, stats_as_of=stats_as_of)

    def _build_doc_response_package(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        episode_id: int | None,
        reason: str,
        source_message_id: str | None,
    ) -> tuple[dict, AnalyzeResponse | None]:
        provided_package = self._document_package_from_llm_result(
            session_id=session_id,
            instruction=instruction,
            llm_result=llm_result,
            episode_id=episode_id,
        )
        if provided_package:
            return provided_package, None

        if self._should_include_slides_step("doc", instruction, llm_result):
            package = self._build_document_package_from_workspace(
                session_id=session_id,
                instruction=instruction,
                llm_result=llm_result,
                workspace_context=workspace_context,
                episode_id=episode_id,
            )
            return package, None

        source_text = self.memory_service.build_discussion_block(
            session_id,
            episode_id=episode_id,
            exclude_message_id=source_message_id,
        ) or workspace_context or instruction
        analysis = self._build_analysis_from_llm(
            session_id=session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent="summary",
            reason=reason or "为文档同步生成结构化沉淀",
        )
        self.memory_service.save_round(
            session_id=session_id,
            analysis=analysis,
            episode_id=episode_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        stats_as_of = self._resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        package = self._document_from_analysis(analysis, instruction, stats_as_of=stats_as_of)
        return package, analysis

    def _document_package_from_llm_result(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        episode_id: int | None,
        stats_as_of: str | None = None,
    ) -> dict | None:
        resolved_stats_as_of = stats_as_of or self._resolve_doc_stats_as_of(session_id, episode_id=episode_id)
        return self.document_package_builder.package_from_llm_result(
            instruction=instruction,
            llm_result=llm_result,
            stats_as_of=resolved_stats_as_of,
        )

    def _is_outline_request(self, instruction: str) -> bool:
        return self.document_package_builder.is_outline_request(instruction)

    def _document_from_analysis(self, analysis: AnalyzeResponse, instruction: str, *, stats_as_of: str | None = None) -> dict:
        return self.document_package_builder.from_analysis(analysis, instruction, stats_as_of=stats_as_of)

    def _document_from_presentation(self, package: dict, instruction: str, *, stats_as_of: str | None = None) -> dict:
        return self.document_package_builder.from_presentation(package, instruction, stats_as_of=stats_as_of)

    def _normalize_doc_sections(self, sections: list[dict]) -> list[dict]:
        return DocTool.normalize_doc_sections(sections)

    def _sync_package_to_session_doc(
        self,
        package: dict,
        *,
        session_id: str,
        episode_id: int | None,
        instruction: str,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> DocumentSyncResult:
        return self._doc_tool().sync_package_to_session_doc(
            package,
            session_id=session_id,
            episode_id=episode_id,
            instruction=instruction,
            task_run_id=task_run_id,
            target_document=target_document,
        )

    def _build_incremental_doc_sections(
        self,
        package: dict,
        *,
        instruction: str,
        previous_snapshot: list[dict] | None = None,
    ) -> tuple[list[dict], list[str], list[str]]:
        return self._doc_tool().build_incremental_doc_sections(
            package,
            instruction=instruction,
            previous_snapshot=previous_snapshot,
        )

    def _build_doc_update_header_lines(
        self,
        *,
        instruction: str,
        targeted_headings: list[str],
    ) -> list[str]:
        return DocTool.build_doc_update_header_lines(
            instruction=instruction,
            targeted_headings=targeted_headings,
        )

    def _resolve_doc_update_targets(
        self,
        instruction: str,
        sections: list[dict],
    ) -> list[str]:
        return DocTool.resolve_doc_update_targets(instruction, sections)

    def _section_snapshot_map(self, snapshot: list[dict]) -> dict[str, list[str]]:
        return DocTool.section_snapshot_map(snapshot)

    def _merge_doc_section_snapshots(
        self,
        previous_snapshot: list[dict],
        updated_sections: list[dict],
        *,
        deleted_headings: list[str] | None = None,
        delete_ranges: list[dict[str, object]] | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> list[dict[str, list[str]]]:
        return DocTool.merge_doc_section_snapshots(
            previous_snapshot,
            updated_sections,
            deleted_headings=deleted_headings,
            delete_ranges=delete_ranges,
            rename_map=rename_map,
        )

    def _build_doc_sync_lines(
        self,
        mode: str,
        document_info: dict,
        *,
        appended_block_count: int | None = None,
        replaced_block_count: int | None = None,
        inserted_block_count: int | None = None,
        changed_headings: list[str] | None = None,
        appended_headings: list[str] | None = None,
        deleted_headings: list[str] | None = None,
        renamed_headings: list[str] | None = None,
        patched_headings: list[str] | None = None,
        unmatched_operation_targets: list[str] | None = None,
        folder_scope: str | None = None,
        folder_url: str | None = None,
        folder_note: str | None = None,
    ) -> list[str]:
        return DocTool.build_doc_sync_lines(
            mode,
            document_info,
            appended_block_count=appended_block_count,
            replaced_block_count=replaced_block_count,
            inserted_block_count=inserted_block_count,
            changed_headings=changed_headings,
            appended_headings=appended_headings,
            deleted_headings=deleted_headings,
            renamed_headings=renamed_headings,
            patched_headings=patched_headings,
            unmatched_operation_targets=unmatched_operation_targets,
            folder_scope=folder_scope,
            folder_url=folder_url,
            folder_note=folder_note,
        )

    def _build_document_artifact(
        self,
        *,
        session_id: str,
        package: dict,
        sync_result: DocumentSyncResult,
        fallback_provider: str,
        task_run_id: str | None = None,
    ) -> dict:
        current_doc = sync_result.document_info or self.session_document_service.get_current_document(session_id) or {}
        sync_lines = sync_result.summary_lines
        if sync_result.status != "ready":
            preview = dict(package)
            try:
                local_artifact = self.office_artifact_service.persist_document_markdown(
                    package,
                    sync_lines=sync_lines,
                    task_run_id=task_run_id,
                    session_id=session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist local document artifact: %s", exc)
                local_artifact = {}
            previous_document = self._previous_document_preview(current_doc)
            sync_preview = self._build_document_sync_preview(
                {},
                url=local_artifact.get("url") if local_artifact else None,
                sync_lines=sync_lines,
                forced_mode=sync_result.mode,
                error=sync_result.error or self._extract_document_sync_error(sync_lines),
            )
            if previous_document:
                sync_preview["previous_document"] = previous_document
            if local_artifact:
                sync_preview["local_artifact"] = local_artifact
            sync_preview["status"] = sync_result.status
            preview["sync"] = sync_preview
            return {
                "artifact_type": "document",
                "provider": fallback_provider,
                "status": sync_result.status,
                "title": str(package.get("title") or "协同文档"),
                "url": local_artifact.get("url") if local_artifact else None,
                "preview": preview,
                "version": sync_result.version,
            }

        url = sync_result.url or self._extract_first_url(sync_lines)
        version = sync_result.version
        preview = dict(package)
        preview["sync"] = self._build_document_sync_preview(current_doc, url=url, sync_lines=sync_lines)
        return {
            "artifact_type": "document",
            "provider": "feishu_doc" if url else fallback_provider,
            "title": str(current_doc.get("title") or package.get("title") or "协同文档"),
            "url": url,
            "preview": preview,
            "version": version,
        }

    def _build_document_sync_preview(
        self,
        document_info: dict,
        *,
        url: str | None,
        sync_lines: list[str],
        forced_mode: str | None = None,
        error: str | None = None,
    ) -> dict:
        mode = str(forced_mode or document_info.get("sync_mode") or "").strip()
        if not mode:
            if any("Updated document" in line for line in sync_lines):
                mode = "updated"
            elif any("No content changes detected" in line for line in sync_lines):
                mode = "noop"
            elif any("Created document" in line for line in sync_lines):
                mode = "created"
            else:
                mode = "local_only"
        preview = {
            "mode": mode,
            "document_id": str(document_info.get("document_id") or "").strip() or None,
            "title": str(document_info.get("title") or "").strip() or None,
            "url": url,
            "version": coerce_positive_int(document_info.get("version")),
            "updated_at": str(document_info.get("updated_at") or "").strip() or None,
            "summary": [line.strip() for line in sync_lines if str(line).strip()],
        }
        if mode == "updated":
            preview["write_strategy"] = "patch_matched_section_bodies"
            preview["strategy_note"] = "Feishu Docx is updated by patching matched section bodies in place, with explicit delete and rename handling when needed."
        elif mode == "noop":
            preview["write_strategy"] = "snapshot_unchanged"
            preview["strategy_note"] = "No changed sections were detected; the existing artifact snapshot remains current."
        if error:
            preview["error"] = error
        return preview

    def _is_document_sync_failed(self, sync_lines: list[str]) -> bool:
        normalized = "\n".join(str(line or "").lower() for line in sync_lines)
        failure_markers = (
            "document sync failed",
            "sync failed",
            "failed:",
            "文档创建失败",
            "同步失败",
            "未启用",
            "disabled",
            "skipped",
        )
        return any(marker in normalized for marker in failure_markers)

    def _extract_document_sync_error(self, sync_lines: list[str]) -> str | None:
        for line in sync_lines:
            text = str(line or "").strip().lstrip("-").strip()
            lowered = text.lower()
            if any(marker in lowered for marker in ("failed", "失败", "disabled", "skipped", "未启用")):
                return text
        return None

    def _previous_document_preview(self, current_doc: dict) -> dict | None:
        if not isinstance(current_doc, dict) or not current_doc.get("document_id"):
            return None
        return {
            "document_id": str(current_doc.get("document_id") or "").strip() or None,
            "title": str(current_doc.get("title") or "").strip() or None,
            "url": str(current_doc.get("url") or "").strip() or None,
            "version": coerce_positive_int(current_doc.get("version")),
            "updated_at": str(current_doc.get("updated_at") or "").strip() or None,
        }

    def _sync_package_to_doc(self, package: dict) -> list[str]:
        if not self.doc_api.is_configured():
            logger.warning("Feishu doc sync skipped because FEISHU_DOC_ENABLED is not enabled.")
            return ["- 飞书文档未启用，请先在环境变量里设置 FEISHU_DOC_ENABLED=true。"]

        try:
            created = self.doc_api.create_document_from_sections(
                str(package.get("title") or "协同文档"),
                package.get("sections") if isinstance(package.get("sections"), list) else [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu doc sync failed: %s", exc)
            return [f"- 文档创建失败：{exc}"]

        lines = [f"- 已创建飞书文档：《{created['title']}》"]
        if created.get("url"):
            lines.append(f"- 文档链接：{created['url']}")
        folder_scope = str(created.get("folder_scope") or "").strip()
        if folder_scope == "explicit" and created.get("folder_url"):
            lines.append(f"- 产出目录：{created['folder_url']}")
        folder_note = str(created.get("folder_note") or "").strip()
        if folder_note:
            lines.append(f"- {folder_note}")
        return lines

    def _format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        return self.response_formatter.format_doc_reply(package, sync_lines)

    def _default_doc_title(self, instruction: str, fallback: str | None = None, stats_as_of: str | None = None) -> str:
        timestamp = stats_as_of or self._doc_title_timestamp()
        if fallback:
            return self._compose_doc_title(f"{settings.feishu_doc_title_prefix} - {fallback.strip()}", stats_as_of=timestamp)
        condensed = " ".join((instruction or "").split()).strip()
        if condensed:
            condensed = condensed[:24]
            return self._compose_doc_title(f"{settings.feishu_doc_title_prefix} - {condensed}", stats_as_of=timestamp)
        return self._compose_doc_title(f"{settings.feishu_doc_title_prefix} - 讨论整理", stats_as_of=timestamp)

    def _doc_title_timestamp(self) -> str:
        return self.document_package_builder.title_timestamp()

    def _resolve_doc_stats_as_of(self, session_id: str, *, episode_id: int | None) -> str | None:
        cutoff_at = self.memory_service.get_discussion_cutoff_at(session_id, episode_id=episode_id)
        if cutoff_at is None:
            return None
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=ZoneInfo("UTC"))
        return cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    def _compose_doc_title(self, base_title: str, *, stats_as_of: str | None) -> str:
        return self.document_package_builder.compose_title(base_title, stats_as_of=stats_as_of)

    def _empty_result(self, session_id: str, mode: str) -> dict:
        return {
            "session_id": session_id,
            "mode": mode,
            "reply_preview": None,
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
            "artifacts": [],
        }

    def _deliver_reply(
        self,
        message: FeishuMessageContext,
        mode: str,
        reply_preview: str | None,
        *,
        analysis: AnalyzeResponse | None,
        episode_id: int | None = None,
        artifacts: list[dict] | None = None,
    ) -> dict:
        reply_sent = False
        reply_error: str | None = None

        if reply_preview and settings.feishu_reply_enabled and message.chat_id:
            try:
                self.message_api.send_text_message(
                    message.chat_id,
                    reply_preview,
                    receive_id_type="chat_id",
                )
                reply_sent = True
            except Exception as exc:  # noqa: BLE001
                reply_error = str(exc)
                logger.exception("Failed to send Feishu reply")

        return {
            "session_id": message.session_id,
            "episode_id": episode_id,
            "mode": mode,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
            "artifacts": artifacts or [],
        }

    def _task_run_title(self, text: str, mode: str | None = None) -> str:
        candidate = " ".join((text or "").split()).strip()
        if candidate:
            candidate = candidate[:40]
        else:
            candidate = "协作任务"
        if mode:
            return f"{candidate} [{mode}]"
        return candidate

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

    def _format_analysis_reply(
        self,
        analysis: AnalyzeResponse,
        mode: str,
    ) -> str:
        return self.response_formatter.format_analysis_reply(analysis, mode)

    def _format_status_reply(self, query: str, tasks: list, payload: dict) -> str:
        return self.response_formatter.format_status_reply(query, tasks, payload)

    def _build_fallback_presentation_package(self, session_id: str) -> dict:
        try:
            tasks = self.memory_service.get_current_tasks(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load tasks for fallback presentation package: %s", exc)
            tasks = []
        try:
            payload = self.memory_service.load_memory_payload(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load memory payload for fallback presentation package: %s", exc)
            payload = {}

        task_lines = [
            f"{task.title}（负责人：{task.owner}，截止：{task.due_date}）"
            for task in tasks[:4]
        ] or ["明确项目目标、角色分工与时间节点"]

        risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
        next_actions = payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else []

        return {
            "theme": "基于飞书群聊讨论的协作推进方案",
            "audience": "项目报名、路演准备、团队协同推进",
            "slides": [
                {
                    "title": "项目背景与目标",
                    "bullets": [
                        "当前要解决的核心问题是什么",
                        "为什么需要用 AI 协助办公协同",
                        "这次输出服务于什么汇报或报名场景",
                    ],
                },
                {"title": "讨论中形成的关键结论", "bullets": task_lines[:3]},
                {"title": "任务拆解与分工", "bullets": task_lines},
                {
                    "title": "当前风险与待确认事项",
                    "bullets": risks[:3] or ["负责人和截止时间仍需进一步确认"],
                },
                {
                    "title": "下一步推进计划",
                    "bullets": next_actions[:3] or ["继续在群里同步进展并更新协作视图"],
                },
            ],
            "emphasis": [
                "AI 不打断日常讨论，而是在需要时统一整理和输出",
                "协作结果可以从 IM 继续延展到文档和演示稿",
            ],
            "assets": ["最新任务清单截图", "关键讨论结论摘要", "时间线或里程碑信息"],
        }

    def _format_help_reply(self, reason: str | None = None) -> str:
        return self.response_formatter.format_help_reply(reason)
