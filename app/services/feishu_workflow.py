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
from app.schemas.feishu_event import FeishuMessageContext
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
        self.analysis_execution = WorkflowAnalysisExecution(self)
        self.canvas_execution = WorkflowCanvasExecution(self)
        self.slides_execution = WorkflowSlidesExecution(self)
        self.status_execution = WorkflowStatusExecution(self)
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
            llm_service=None,
        )

    def _should_run_dag_planner(self, route_decision: RouteDecision) -> bool:
        if route_decision.needs_clarification:
            return True
        if route_decision.route == "unknown":
            return True
        if len(route_decision.requested_outputs) > 1:
            return True
        return route_decision.source != "rule"

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
        return tasks_from_document_snapshot(snapshot)

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
        )
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
