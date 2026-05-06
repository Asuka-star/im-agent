from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.graph.builder import build_workspace_graph
from app.services.graph.state import ContextPack, GraphMessage, ReplyPackage, ReviewReport, WorkflowGraphState
from app.services.tools.doc_tool import DocTool
from app.services.tools.task_operation_tool import TaskOperationTool

logger = logging.getLogger(__name__)


class GraphRunner:
    """Runs the LangGraph orchestration path in shadow mode for now."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow
        self.shadow_graph = build_workspace_graph(workflow, execute_workers=False)
        self.execution_graph = build_workspace_graph(workflow, execute_workers=True)

    def run_shadow(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str | None = None,
        workspace_context: str = "",
        legacy_route: dict[str, Any] | None = None,
        current_document: dict[str, Any] | None = None,
    ) -> WorkflowGraphState:
        initial_state = self._initial_state(
            message,
            task_run_id=task_run_id,
            workspace_context=workspace_context,
            legacy_route=legacy_route,
            current_document=current_document,
        )
        try:
            final_payload = self.shadow_graph.invoke(initial_state.model_dump(mode="json"))
            final_state = WorkflowGraphState.model_validate(final_payload)
            self._persist_shadow_state(final_state)
            return final_state
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LangGraph shadow run failed: task_run_id=%s message_id=%s error=%s",
                task_run_id,
                message.message_id,
                exc,
            )
            failed_state = initial_state.model_copy(
                update={
                    "errors": [
                        *initial_state.errors,
                        {"node": "graph.runner", "error": str(exc)},
                    ],
                    "trace": [
                        *initial_state.trace,
                        {"node": "graph.runner", "status": "failed", "error": str(exc)},
                    ],
                }
            )
            self._persist_shadow_state(failed_state, error=str(exc))
            return failed_state

    def run_task_graph(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str | None,
        workspace_context: str,
        active_episode_id: int | None,
        legacy_route: dict[str, Any] | None = None,
        current_document: dict[str, Any] | None = None,
    ) -> dict | None:
        initial_state = self._initial_state(
            message,
            task_run_id=task_run_id,
            workspace_context=workspace_context,
            legacy_route=legacy_route,
            active_episode_id=active_episode_id,
            current_document=current_document,
        )
        try:
            final_payload = self.execution_graph.invoke(initial_state.model_dump(mode="json"))
            final_state = WorkflowGraphState.model_validate(final_payload)
            self._persist_execution_state(final_state)
            if not self._can_return_graph_result(final_state):
                return None
            review = final_state.review or ReviewReport()
            if review.needs_clarification and review.clarification:
                return self._pause_for_graph_confirmation(
                    message,
                    active_episode_id=active_episode_id,
                    final_state=final_state,
                    workspace_context=workspace_context,
                )
            return self._deliver_graph_reply(message, final_state, active_episode_id=active_episode_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LangGraph task execution failed, falling back to legacy: task_run_id=%s message_id=%s error=%s",
                task_run_id,
                message.message_id,
                exc,
            )
            failed_state = initial_state.model_copy(
                update={
                    "errors": [
                        *initial_state.errors,
                        {"node": "graph.runner", "error": str(exc)},
                    ],
                    "trace": [
                        *initial_state.trace,
                        {"node": "graph.runner", "status": "failed", "error": str(exc)},
                    ],
                }
            )
            self._persist_execution_state(failed_state, error=str(exc))
            return None

    def resume_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str = "user",
    ) -> dict | None:
        task_run_service = getattr(self.workflow, "task_run_service", None)
        if task_run_service is None:
            return None
        metadata = task_run_service.get_task_run_metadata(task_run_id)
        resume_payload = metadata.get("resume_after_graph_confirmation")
        if not isinstance(resume_payload, dict):
            return None
        expected_confirmation_id = str(resume_payload.get("confirmation_id") or "").strip()
        if expected_confirmation_id and expected_confirmation_id != confirmation_id:
            return None
        state_payload = resume_payload.get("state") if isinstance(resume_payload.get("state"), dict) else None
        if not state_payload:
            return None
        if self._is_cancel_answer(answer_value):
            return self._cancel_graph_resume(
                task_run_id,
                metadata=metadata,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=answered_by,
            )
        state = WorkflowGraphState.model_validate(state_payload)
        state = self._apply_confirmation_answer(state, answer_value, confirmation_id=confirmation_id, answered_by=answered_by)
        try:
            task_run_service.upsert_step(
                task_run_id,
                step_key="graph.confirmation_resume",
                title="LangGraph confirmation resume",
                step_type="graph_confirmation",
                status="running",
                output_payload={
                    "confirmation_id": confirmation_id,
                    "answer_value": answer_value,
                    "answered_by": answered_by,
                },
            )
            task_run_service.update_task_run(task_run_id, stage="graph_confirmation_resuming", status="running")
            final_payload = self.execution_graph.invoke(state.model_dump(mode="json"))
            final_state = WorkflowGraphState.model_validate(final_payload)
            self._persist_execution_state(final_state)
            review = final_state.review or ReviewReport()
            if review.needs_clarification and review.clarification:
                self._clear_graph_resume_metadata(task_run_id, metadata, confirmation_id, answer_value, answered_by)
                message = self._context_to_message(final_state.message)
                return self._pause_for_graph_confirmation(
                    message,
                    active_episode_id=final_state.active_episode_id,
                    final_state=final_state,
                    workspace_context=final_state.context.excerpt if final_state.context else "",
                )
            if not self._can_return_graph_result(final_state):
                task_run_service.upsert_step(
                    task_run_id,
                    step_key="graph.confirmation_resume",
                    title="LangGraph confirmation resume",
                    step_type="graph_confirmation",
                    status="skipped",
                    output_payload={
                        "confirmation_id": confirmation_id,
                        "answer_value": answer_value,
                        "answered_by": answered_by,
                        "reason": "graph resume produced no returnable result; legacy resume metadata preserved",
                    },
                )
                return None
            message = self._context_to_message(final_state.message)
            result = self._deliver_graph_reply(message, final_state, active_episode_id=final_state.active_episode_id)
            self.workflow.result_persistence.persist_task_run_result(
                task_run_id,
                message_text=final_state.message.text,
                result=result,
                session_id=final_state.message.session_id,
                source_message_id=final_state.message.message_id,
            )
            self._clear_graph_resume_metadata(task_run_id, metadata, confirmation_id, answer_value, answered_by)
            task_run_service.upsert_step(
                task_run_id,
                step_key="graph.confirmation_resume",
                title="LangGraph confirmation resume",
                step_type="graph_confirmation",
                status="done",
                output_payload={
                    "confirmation_id": confirmation_id,
                    "answer_value": answer_value,
                    "answered_by": answered_by,
                    "mode": result.get("mode"),
                    "artifact_count": len(result.get("artifacts", [])),
                },
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LangGraph confirmation resume failed: task_run_id=%s confirmation_id=%s error=%s",
                task_run_id,
                confirmation_id,
                exc,
            )
            task_run_service.upsert_step(
                task_run_id,
                step_key="graph.confirmation_resume",
                title="LangGraph confirmation resume",
                step_type="graph_confirmation",
                status="failed",
                error=str(exc),
            )
            task_run_service.update_task_run(task_run_id, stage="failed", status="failed", latest_error=str(exc))
            return None

    def _persist_shadow_state(self, state: WorkflowGraphState, *, error: str | None = None) -> None:
        task_run_id = state.task_run_id
        task_run_service = getattr(self.workflow, "task_run_service", None)
        if not task_run_id or task_run_service is None:
            return
        payload = self._shadow_payload(state)
        status = "failed" if error else "done"
        task_run_service.upsert_step(
            task_run_id,
            step_key="graph.shadow_command",
            title="LangGraph shadow command",
            step_type="graph",
            status=status,
            output_payload=payload,
            error=error,
        )
        task_run_service.merge_task_run_metadata(
            task_run_id,
            {
                "langgraph_shadow": payload,
            },
        )

    def _persist_execution_state(self, state: WorkflowGraphState, *, error: str | None = None) -> None:
        task_run_id = state.task_run_id
        task_run_service = getattr(self.workflow, "task_run_service", None)
        if not task_run_id or task_run_service is None:
            return
        payload = self._shadow_payload(state)
        payload["worker_results"] = {
            key: value.model_dump(mode="json")
            for key, value in state.worker_results.items()
        }
        payload["review"] = state.review.model_dump(mode="json") if state.review else None
        payload["reply"] = state.reply.model_dump(mode="json") if state.reply else None
        task_run_service.upsert_step(
            task_run_id,
            step_key="graph.execution",
            title="LangGraph execution",
            step_type="graph",
            status="failed" if error else "done",
            output_payload=payload,
            error=error,
        )
        task_run_service.merge_task_run_metadata(
            task_run_id,
            {
                "langgraph_execution": payload,
            },
        )

    def _pause_for_graph_confirmation(
        self,
        message: FeishuMessageContext,
        *,
        active_episode_id: int | None,
        final_state: WorkflowGraphState,
        workspace_context: str,
    ) -> dict:
        review = final_state.review or ReviewReport()
        clarification = review.clarification or {
            "question": "请确认下一步怎么处理。",
            "reason": "LangGraph worker requested clarification.",
            "options": ["确认继续", "取消操作"],
            "blocking": True,
        }
        result = self.workflow._pause_for_clarification(
            message,
            intent=self._result_mode(final_state),
            clarification=clarification,
            active_episode_id=active_episode_id,
            task_run_id=final_state.task_run_id,
            workspace_context=workspace_context,
            artifacts=[],
        )
        confirmation_id = str(result.get("confirmation_id") or "").strip()
        if confirmation_id and final_state.task_run_id:
            self._store_graph_resume_state(
                final_state,
                confirmation_id=confirmation_id,
                workspace_context=workspace_context,
                clarification=clarification,
            )
        return result

    def _store_graph_resume_state(
        self,
        state: WorkflowGraphState,
        *,
        confirmation_id: str,
        workspace_context: str,
        clarification: dict[str, Any],
    ) -> None:
        task_run_service = getattr(self.workflow, "task_run_service", None)
        if not state.task_run_id or task_run_service is None:
            return
        payload = {
            "confirmation_id": confirmation_id,
            "instruction": state.message.text,
            "workspace_context": workspace_context,
            "active_episode_id": state.active_episode_id,
            "question": clarification.get("question"),
            "reason": clarification.get("reason"),
            "options": clarification.get("options") if isinstance(clarification.get("options"), list) else [],
            "state": state.model_dump(mode="json"),
        }
        task_run_service.merge_task_run_metadata(
            state.task_run_id,
            {
                "resume_after_graph_confirmation": payload,
            },
        )
        task_run_service.upsert_step(
            state.task_run_id,
            step_key="graph.pending_confirmation",
            title="LangGraph pending confirmation",
            step_type="graph_confirmation",
            status="pending",
            output_payload={
                "confirmation_id": confirmation_id,
                "question": payload["question"],
                "reason": payload["reason"],
                "options": payload["options"],
            },
        )

    @staticmethod
    def _shadow_payload(state: WorkflowGraphState) -> dict[str, Any]:
        payload = {
            "command": state.command.model_dump(mode="json") if state.command else None,
            "plan": state.plan.model_dump(mode="json") if state.plan else None,
            "legacy_route": state.context.legacy_route if state.context else None,
            "errors": state.errors,
            "trace": state.trace,
        }
        comparison = GraphRunner._shadow_comparison(state)
        if comparison:
            payload["comparison"] = comparison
        return payload

    @staticmethod
    def _shadow_comparison(state: WorkflowGraphState) -> dict[str, Any] | None:
        command = state.command
        legacy_route = state.context.legacy_route if state.context else None
        if command is None or not isinstance(legacy_route, dict):
            return None

        legacy_route_name = str(legacy_route.get("route") or "").strip().lower()
        graph_route_name = GraphRunner._route_from_command(command)
        legacy_outputs = GraphRunner._normalized_outputs(legacy_route.get("requested_outputs"))
        graph_outputs = GraphRunner._normalized_outputs(command.requested_outputs)
        legacy_compare_outputs = legacy_outputs or (
            [legacy_route_name] if legacy_route_name in {"doc", "slides", "canvas"} else []
        )
        graph_compare_outputs = graph_outputs or (
            [graph_route_name] if graph_route_name in {"doc", "slides", "canvas"} else []
        )

        route_match = bool(
            legacy_route_name
            and graph_route_name
            and (
                legacy_route_name == graph_route_name
                or legacy_route_name in graph_compare_outputs
                or graph_route_name in legacy_compare_outputs
            )
        )
        outputs_match = set(legacy_compare_outputs) == set(graph_compare_outputs)
        if not legacy_compare_outputs and not graph_compare_outputs:
            outputs_match = True
        legacy_needs_clarification = bool(legacy_route.get("needs_clarification"))
        needs_clarification_match = legacy_needs_clarification == command.needs_clarification
        legacy_confidence = GraphRunner._float_or_none(legacy_route.get("confidence"))
        graph_confidence = GraphRunner._float_or_none(command.confidence)
        confidence_delta = (
            round(graph_confidence - legacy_confidence, 4)
            if graph_confidence is not None and legacy_confidence is not None
            else None
        )
        notes: list[str] = []
        if not route_match:
            notes.append("route_mismatch")
        if not outputs_match:
            notes.append("requested_outputs_mismatch")
        if not needs_clarification_match:
            notes.append("clarification_mismatch")

        return {
            "status": "match" if route_match and outputs_match and needs_clarification_match else "diverged",
            "route_match": route_match,
            "outputs_match": outputs_match,
            "needs_clarification_match": needs_clarification_match,
            "legacy_route": legacy_route_name,
            "graph_route": graph_route_name,
            "legacy_outputs": legacy_outputs,
            "graph_outputs": graph_outputs,
            "legacy_confidence": legacy_confidence,
            "graph_confidence": graph_confidence,
            "confidence_delta": confidence_delta,
            "notes": notes,
        }

    @staticmethod
    def _route_from_command(command: WorkspaceCommand) -> str:
        if command.route and command.route != "unknown":
            return command.route
        if command.operation == "help":
            return "help"
        if command.operation == "analyze" and command.object in {"summary", "tasks", "risks"}:
            return command.object
        if command.operation == "read" and command.object in {"task", "tasks"}:
            return "status"
        if command.operation in {"create", "update", "remove", "complete", "assign"} and command.object in {"task", "tasks"}:
            return "tasks"
        if command.operation == "generate":
            outputs = GraphRunner._normalized_outputs(command.requested_outputs)
            if outputs:
                return outputs[0]
        if command.object in {"doc", "slides", "canvas", "delivery"}:
            return command.object
        if command.operation == "recommend" and command.object == "workspace":
            return "status"
        return "unknown"

    @staticmethod
    def _normalized_outputs(value: Any) -> list[str]:
        raw_items = [value] if isinstance(value, str) else value if isinstance(value, list | tuple) else []
        outputs: list[str] = []
        aliases = {
            "document": "doc",
            "feishu_doc": "doc",
            "ppt": "slides",
            "presentation": "slides",
            "deck": "slides",
            "whiteboard": "canvas",
            "diagram": "canvas",
            "flowchart": "canvas",
        }
        for item in raw_items:
            output = aliases.get(str(item or "").strip().lower(), str(item or "").strip().lower())
            if output in {"doc", "slides", "canvas"} and output not in outputs:
                outputs.append(output)
        return outputs

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _message_from_context(message: FeishuMessageContext) -> GraphMessage:
        return GraphMessage(
            message_id=getattr(message, "message_id", None),
            session_id=getattr(message, "session_id"),
            chat_id=getattr(message, "chat_id", None),
            chat_type=getattr(message, "chat_type", None) or "group",
            sender_id=getattr(message, "sender_id", None),
            text=getattr(message, "text", ""),
            raw_text=getattr(message, "raw_text", getattr(message, "text", "")),
            is_mentioned=bool(getattr(message, "is_mentioned", False)),
        )

    def _initial_state(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str | None,
        workspace_context: str,
        legacy_route: dict[str, Any] | None,
        active_episode_id: int | None = None,
        current_document: dict[str, Any] | None = None,
    ) -> WorkflowGraphState:
        return WorkflowGraphState(
            message=self._message_from_context(message),
            task_run_id=task_run_id,
            active_episode_id=active_episode_id,
            context=ContextPack(
                session_id=message.session_id,
                excerpt=self._compact_context(workspace_context),
                legacy_route=legacy_route,
                current_document=current_document,
            ),
        )

    @staticmethod
    def _can_return_graph_result(state: WorkflowGraphState) -> bool:
        command = state.command
        plan = state.plan
        if command is None:
            return False
        review = state.review or ReviewReport()
        if review.needs_clarification:
            return True
        if plan is None:
            return False
        executable_workers = {"task", "analysis", "doc", "slides", "canvas", "delivery", "help", "reply"}
        if any(step.worker not in executable_workers for step in plan.steps):
            return False
        if not review.ok and not GraphRunner._has_successful_worker(state):
            return False
        return state.reply is not None and bool(state.reply.text)

    @staticmethod
    def _has_successful_worker(state: WorkflowGraphState) -> bool:
        return any(result.ok for result in state.worker_results.values())

    def _deliver_graph_reply(
        self,
        message: FeishuMessageContext,
        state: WorkflowGraphState,
        *,
        active_episode_id: int | None,
    ) -> dict:
        reply = state.reply or ReplyPackage()
        analysis = None
        if isinstance(reply.analysis, dict):
            try:
                analysis = AnalyzeResponse.model_validate(reply.analysis)
            except Exception:  # noqa: BLE001
                analysis = None
        mode = self._result_mode(state)
        return self.workflow.reply_sender.deliver_reply(
            message,
            mode,
            reply.text,
            analysis=analysis,
            episode_id=active_episode_id,
            artifacts=reply.artifacts,
            task_run_id=state.task_run_id,
        )

    def _apply_confirmation_answer(
        self,
        state: WorkflowGraphState,
        answer_value: str,
        *,
        confirmation_id: str,
        answered_by: str,
    ) -> WorkflowGraphState:
        command = state.command
        answer = str(answer_value or "").strip()
        context = state.context
        context_patch = self._context_patch_from_answer(state, answer)
        if context is not None and context_patch:
            context = context.model_copy(update=context_patch)
        if command is not None:
            command = command.model_copy(
                update={
                    "needs_clarification": False,
                    "clarification_question": None,
                    **self._command_patch_from_answer(state, answer),
                }
            )
        return state.model_copy(
            update={
                "command": command,
                "context": context,
                "review": None,
                "reply": None,
                "worker_results": {},
                "confirmation_answer": {
                    "confirmation_id": confirmation_id,
                    "answer_value": answer,
                    "answered_by": answered_by,
                    "confirmed": True,
                },
                "trace": [
                    *state.trace,
                    {
                        "node": "graph.confirmation_resume",
                        "status": "answered",
                        "confirmation_id": confirmation_id,
                        "answer_value": answer,
                        "answered_by": answered_by,
                    },
                ],
            }
        )

    def _context_patch_from_answer(self, state: WorkflowGraphState, answer: str) -> dict[str, Any]:
        command = state.command
        context = state.context
        if command is None or context is None:
            return {}
        if command.object != "doc" or command.operation not in {"revise", "update"}:
            return {}
        if isinstance(context.current_document, dict):
            return {}
        target_document = self._resolve_document_from_answer(state, answer)
        if not isinstance(target_document, dict):
            return {}
        excerpt = context.excerpt
        doc_context = DocTool.format_current_document_context(target_document)
        join_context_blocks = getattr(self.workflow, "_join_context_blocks", None)
        if callable(join_context_blocks):
            excerpt = join_context_blocks(excerpt, doc_context)
        elif doc_context:
            excerpt = "\n\n".join(block for block in (excerpt, doc_context) if block)
        return {
            "current_document": target_document,
            "excerpt": excerpt,
        }

    def _resolve_document_from_answer(self, state: WorkflowGraphState, answer: str) -> dict | None:
        resolver = getattr(self.workflow, "_resolve_target_document_for_instruction", None)
        if not callable(resolver):
            return None
        session_id = state.message.session_id
        candidates = [
            f"{state.message.text}\n{answer}".strip(),
            answer,
        ]
        for instruction in candidates:
            if not instruction:
                continue
            try:
                target_document = resolver(session_id, instruction)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LangGraph document selection resolution failed: %s", exc)
                continue
            if isinstance(target_document, dict):
                return target_document
        return None

    def _command_patch_from_answer(self, state: WorkflowGraphState, answer: str) -> dict[str, Any]:
        command = state.command
        if command is None:
            return {}
        if command.operation == "generate" and not command.requested_outputs:
            outputs = self._outputs_from_answer(answer)
            if outputs:
                return {"requested_outputs": outputs, "operation": "generate", "object": "workspace"}
        if command.operation == "remove":
            candidate = self._candidate_from_answer(state, answer)
            if candidate:
                return {
                    "target_text": str(candidate.get("title") or command.target_text),
                    "target_owner": str(candidate.get("owner") or command.target_owner or "").strip() or None,
                }
        return {}

    @staticmethod
    def _outputs_from_answer(answer: str) -> list[str]:
        lowered = answer.lower()
        outputs: list[str] = []
        markers = (
            ("doc", ("文档", "材料", "doc", "document")),
            ("slides", ("ppt", "slides", "presentation", "演示")),
            ("canvas", ("canvas", "画布", "流程图", "白板", "diagram", "flowchart")),
        )
        for output, aliases in markers:
            if any(alias in lowered or alias in answer for alias in aliases):
                outputs.append(output)
        return outputs

    @staticmethod
    def _candidate_from_answer(state: WorkflowGraphState, answer: str) -> dict[str, Any] | None:
        review = state.review or ReviewReport()
        clarification = review.clarification if isinstance(review.clarification, dict) else {}
        raw_candidates = clarification.get("candidates") if isinstance(clarification.get("candidates"), list) else []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            try:
                task = TaskItem.model_validate(item)
            except Exception:
                continue
            label = TaskOperationTool.task_option_label(task)
            if label == answer or label in answer or task.title in answer:
                return task.model_dump(mode="json")
        return None

    def _clear_graph_resume_metadata(
        self,
        task_run_id: str,
        metadata: dict,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
    ) -> None:
        latest_metadata = self.workflow.task_run_service.get_task_run_metadata(task_run_id)
        metadata = {**dict(metadata), **dict(latest_metadata)}
        metadata.pop("resume_after_graph_confirmation", None)
        legacy_resume = metadata.get("resume_after_confirmation")
        if isinstance(legacy_resume, dict) and str(legacy_resume.get("confirmation_id") or "").strip() == confirmation_id:
            metadata.pop("resume_after_confirmation", None)
        metadata["last_graph_confirmation"] = {
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
        }
        self.workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)

    def _cancel_graph_resume(
        self,
        task_run_id: str,
        *,
        metadata: dict,
        confirmation_id: str,
        answer_value: str,
        answered_by: str,
    ) -> dict:
        session_id = ""
        resume_payload = metadata.get("resume_after_graph_confirmation")
        if isinstance(resume_payload, dict):
            state_payload = resume_payload.get("state")
            if isinstance(state_payload, dict):
                message_payload = state_payload.get("message")
                if isinstance(message_payload, dict):
                    session_id = str(message_payload.get("session_id") or "")
        self._clear_graph_resume_metadata(task_run_id, metadata, confirmation_id, answer_value, answered_by)
        reply_preview = f"已取消本次 LangGraph 确认操作：{answer_value or '取消'}。"
        self.workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="graph.confirmation_resume",
            title="LangGraph confirmation resume",
            step_type="graph_confirmation",
            status="done",
            output_payload={
                "confirmation_id": confirmation_id,
                "answer_value": answer_value,
                "answered_by": answered_by,
                "cancelled": True,
            },
        )
        self.workflow.task_run_service.update_task_run(
            task_run_id,
            stage="graph_confirmation_cancelled",
            status="completed",
            latest_summary=reply_preview,
            latest_reply_preview=reply_preview,
        )
        return {
            "session_id": session_id,
            "mode": "graph_confirmation_cancelled",
            "reply_preview": reply_preview,
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
            "artifacts": [],
        }

    @staticmethod
    def _is_cancel_answer(answer_value: str) -> bool:
        answer = str(answer_value or "").strip().lower()
        return answer in {"cancel", "cancelled", "canceled", "取消", "取消操作"} or "取消" in answer

    @staticmethod
    def _context_to_message(message: GraphMessage) -> FeishuMessageContext:
        return FeishuMessageContext(
            message_id=message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            message_type="text",
            session_id=message.session_id,
            sender_id=message.sender_id or "",
            text=message.text,
            raw_text=message.raw_text or message.text,
            is_mentioned=message.is_mentioned,
        )

    @staticmethod
    def _result_mode(state: WorkflowGraphState) -> str:
        command = state.command
        if command and command.route in {"summary", "tasks", "risks", "delivery", "help"}:
            return command.route
        if command and command.operation == "help":
            return "help"
        if command and command.operation == "read":
            return "status"
        plan = state.plan
        workers = [step.worker for step in plan.steps] if plan else []
        if len(workers) == 1 and workers[0] == "analysis":
            return "summary"
        if len(workers) == 1 and workers[0] == "help":
            return "help"
        if len(workers) == 1 and workers[0] == "reply":
            return "help"
        if workers and workers[-1] == "delivery":
            return "delivery"
        if len(workers) == 1 and workers[0] in {"doc", "slides", "canvas"}:
            return workers[0]
        if any(worker in {"doc", "slides", "canvas"} for worker in workers):
            return "artifacts"
        return "tasks"

    @staticmethod
    def _compact_context(value: str, *, max_chars: int = 3000) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[: max_chars // 2].rstrip()}\n...\n{text[-max_chars // 2 :].lstrip()}"
