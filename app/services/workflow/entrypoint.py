from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import settings
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.requirement import RequirementResolveCandidate
from app.services.requirement_resolver import RequirementResolveInput

logger = logging.getLogger(__name__)


class WorkflowEntrypoint:
    """Coordinates incoming Feishu messages before delegating concrete work."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    @staticmethod
    def _empty_result(session_id: str, mode: str) -> dict:
        return {
            "session_id": session_id,
            "mode": mode,
            "reply_preview": None,
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
            "artifacts": [],
        }

    def _merge_task_run_metadata(self, task_run_id: str, patch: dict) -> None:
        try:
            self.workflow.task_run_service.merge_task_run_metadata(task_run_id, patch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to merge task run metadata: task_run_id=%s error=%s", task_run_id, exc)

    def handle_message(self, message: FeishuMessageContext) -> dict:
        workflow = self.workflow
        started_at = time.perf_counter()
        active_episode_id: int | None = None
        if message.chat_type == "group":
            try:
                workflow.memory_service.register_team_group_session(
                    workflow._team_id_for_message(message),
                    message.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to register group session for team memory: %s", exc)
        if message.chat_type == "group" and not message.is_mentioned:
            active_episode = workflow.memory_service.ensure_active_episode(message.session_id)
            active_episode_id = active_episode.id

        workflow._ensure_sender_alias(message)

        saved_content = message.text or message.raw_text
        if not saved_content and message.message_type == "audio":
            saved_content = "[语音消息]"

        workflow.memory_service.save_user_message(
            session_id=message.session_id,
            message_id=message.message_id,
            sender_id=message.sender_id,
            content=saved_content,
            episode_id=active_episode_id,
            mentioned_users=[user.model_dump() for user in message.mentioned_users],
            embed=False,
        )
        lifecycle_info = {}
        try:
            lifecycle_info = workflow.memory_service.get_message_lifecycle_info(message.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load message lifecycle state after save: message_id=%s error=%s", message.message_id, exc)
        if lifecycle_info.get("status") == "recalled":
            logger.info(
                "Skipping recalled message event that arrived after lifecycle callback: message_id=%s",
                message.message_id,
            )
            return self._empty_result(message.session_id, "recalled_message_skipped")

        try:
            stored_content = workflow.memory_service.get_user_message_content(message.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load persisted message content after save: message_id=%s error=%s", message.message_id, exc)
            stored_content = None
        if stored_content and stored_content != saved_content:
            if hasattr(message, "model_copy"):
                message = message.model_copy(update={"text": stored_content, "raw_text": stored_content})
            else:
                message.text = stored_content
                message.raw_text = stored_content
        logger.info(
            "Workflow stage completed: message_id=%s stage=save_user_message elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )

        if message.chat_type == "group" and not message.is_mentioned:
            requirement_resolution = self._observe_passive_requirement_discussion(
                message,
                active_episode_id=active_episode_id,
            )
            logger.info(
                "Workflow stage completed: message_id=%s stage=buffer_return total_elapsed_ms=%.1f",
                message.message_id,
                (time.perf_counter() - started_at) * 1000,
            )
            result = self._empty_result(message.session_id, "buffer")
            if requirement_resolution is not None:
                result["requirement_resolution"] = requirement_resolution.model_dump(mode="json")
            return result

        requirement_resolution = workflow.requirement_resolver.resolve(message)
        requirement_id = requirement_resolution.requirement_id if requirement_resolution.action in {"bind", "create"} else None
        requirement_metadata = {
            "requirement_resolution": requirement_resolution.model_dump(mode="json"),
        }
        task_run = workflow.task_run_service.create_task_run(
            session_id=message.session_id,
            title=workflow._task_run_title(message.text),
            source_type=message.chat_type or "unknown",
            requirement_id=requirement_id,
            source_ref=message.chat_id,
            trigger_message_id=message.message_id,
            created_by=message.sender_id,
            metadata={
                "event_id": message.event_id,
                "chat_id": message.chat_id,
                "is_mentioned": message.is_mentioned,
                **requirement_metadata,
                **workflow._task_run_lifecycle_metadata(None),
            },
        )
        if requirement_resolution.action == "bind" and requirement_id:
            workflow.requirement_service.bind_task_run(
                task_run_id=task_run.task_run_id,
                requirement_id=requirement_id,
                session_id=message.session_id,
                message_id=message.message_id,
                source_type="im",
            )
        workflow.task_run_service.upsert_step(
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
        workflow.task_run_service.update_task_run(task_run.task_run_id, status="running", stage="building_context")

        if requirement_resolution.action == "clarify":
            result = self._pause_for_requirement_clarification(
                message,
                task_run_id=task_run.task_run_id,
                requirement_resolution=requirement_resolution,
                active_episode_id=active_episode_id,
            )
            result["task_run_id"] = task_run.task_run_id
            return result

        transcription_notice = getattr(message, "transcription_notice", None)
        if transcription_notice:
            result = workflow.reply_sender.deliver_reply(
                message,
                "speech_notice",
                transcription_notice,
                analysis=None,
            )
            workflow.task_run_service.upsert_step(
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
            workflow.task_run_service.update_task_run(
                task_run.task_run_id,
                intent=result["mode"],
                title=workflow._task_run_title(message.text, result["mode"]),
                stage="delivered",
                status="completed",
                latest_summary=workflow._condense_text(result.get("reply_preview")),
                latest_reply_preview=result.get("reply_preview"),
                latest_error=result.get("reply_error"),
            )
            self._merge_task_run_metadata(
                task_run.task_run_id,
                workflow._task_run_lifecycle_metadata(result["mode"]),
            )
            result["task_run_id"] = task_run.task_run_id
            return result

        try:
            result = workflow._handle_mentioned_request(message, task_run_id=task_run.task_run_id)
        except Exception as exc:  # noqa: BLE001
            workflow.task_run_service.update_task_run(
                task_run.task_run_id,
                status="failed",
                stage="failed",
                latest_error=str(exc),
            )
            workflow.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成处理结果",
                step_type="workflow",
                status="failed",
                error=str(exc),
            )
            raise

        if result["reply_preview"]:
            workflow.memory_service.save_assistant_message(
                session_id=message.session_id,
                content=result["reply_preview"],
                episode_id=result.get("episode_id"),
                source_message_id=message.message_id,
                embed=False,
            )
        workflow.result_persistence.persist_artifacts(task_run.task_run_id, result.get("artifacts", []))
        summary_text = (
            result["analysis"].summary
            if result.get("analysis") is not None
            else workflow._condense_text(result.get("reply_preview"))
        )
        response_step_status = str(result.get("response_step_status") or "done")
        final_stage = str(result.get("task_run_stage") or "delivered")
        final_status = str(result.get("task_run_status") or "completed")
        workflow.task_run_service.upsert_step(
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
            workflow.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="artifact_persisted",
                title="记录协作产物",
                step_type="artifact",
                status="done",
                output_payload={"artifact_count": len(result["artifacts"])},
            )
        workflow.task_run_service.update_task_run(
            task_run.task_run_id,
            intent=result["mode"],
            title=workflow._task_run_title(message.text, result["mode"]),
            stage=final_stage,
            status=final_status,
            latest_summary=summary_text,
            latest_reply_preview=result.get("reply_preview"),
            latest_error=result.get("reply_error"),
        )
        self._merge_task_run_metadata(
            task_run.task_run_id,
            workflow._task_run_lifecycle_metadata(result["mode"]),
        )
        workflow.result_persistence.store_next_action_recommendations(task_run.task_run_id)
        workflow.requirement_service.update_current_artifacts_from_task_run(task_run.task_run_id)
        result["task_run_id"] = task_run.task_run_id
        logger.info(
            "Workflow stage completed: message_id=%s stage=workflow_done total_elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return result

    def _observe_passive_requirement_discussion(
        self,
        message: FeishuMessageContext,
        *,
        active_episode_id: int | None,
    ) -> Any | None:
        workflow = self.workflow
        try:
            resolution = workflow.requirement_resolver.resolve(
                RequirementResolveInput(
                    session_id=message.session_id,
                    text=message.text or message.raw_text,
                    sender_id=message.sender_id,
                    message_id=message.message_id,
                    source_type="im_passive_group",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Passive requirement observation failed: message_id=%s error=%s", message.message_id, exc)
            return None

        if resolution.action == "bind" and resolution.requirement_id:
            try:
                workflow.requirement_service.record_source(
                    requirement_id=resolution.requirement_id,
                    session_id=message.session_id,
                    message_id=message.message_id,
                    source_type="im_passive_group",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to record passive requirement source: requirement_id=%s message_id=%s error=%s",
                    resolution.requirement_id,
                    message.message_id,
                    exc,
                )
        logger.info(
            "Passive requirement observation: message_id=%s action=%s requirement_id=%s confidence=%.2f episode_id=%s",
            message.message_id,
            resolution.action,
            resolution.requirement_id,
            resolution.confidence,
            active_episode_id,
        )
        return resolution

    def _pause_for_requirement_clarification(
        self,
        message: FeishuMessageContext,
        *,
        task_run_id: str,
        requirement_resolution: Any,
        active_episode_id: int | None,
    ) -> dict:
        workflow = self.workflow
        candidates = self._recent_requirement_candidates(
            message.session_id,
            list(getattr(requirement_resolution, "candidates", []) or []),
        )
        options = [f"{index}. {item.title}" for index, item in enumerate(candidates[:5], start=1)]
        options.append("其他 / 新建一个需求")
        question = "我需要确认这次操作属于以下哪个需求，还是其他新需求。"
        clarification = {
            "question": question,
            "reason": getattr(requirement_resolution, "reason", "") or "当前请求可能对应多个需求工作区。",
            "options": options,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
        confirmation = workflow.task_run_service.create_confirmation(
            task_run_id,
            prompt=question,
            options=options,
        )
        workflow.task_run_service.update_task_run(
            task_run_id,
            stage="requirement_clarification",
            status="waiting_confirmation",
        )
        workflow.task_run_service.upsert_step(
            task_run_id,
            step_key="requirement_confirmation",
            title="确认需求归属",
            step_type="confirmation",
            status="pending",
            output_payload={
                "question": question,
                "reason": clarification["reason"],
                "options": options,
                "candidates": [item.model_dump(mode="json") for item in candidates],
            },
        )
        metadata = workflow.task_run_service.get_task_run_metadata(task_run_id)
        metadata["requirement_confirmation"] = {
            "confirmation_id": confirmation.confirmation_id,
            "instruction": message.text,
            "active_episode_id": active_episode_id,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "new_option": "新建一个需求",
        }
        workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)
        reply_preview = workflow.response_formatter.format_clarification_reply(
            intent="需求归属",
            clarification=clarification,
        )
        result = workflow.reply_sender.deliver_reply(
            message,
            "requirement",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=[],
            append_next_actions=False,
            task_run_id=task_run_id,
        )
        if settings.feishu_reply_enabled and settings.feishu_reply_card_enabled:
            try:
                result["reply_card_sent"] = workflow.reply_sender.send_clarification_card(
                    message,
                    intent="requirement",
                    clarification=clarification,
                    task_run_id=task_run_id,
                    confirmation_id=confirmation.confirmation_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to send Feishu requirement clarification card")
                result["reply_card_error"] = str(exc)
        result["pending_confirmation"] = True
        result["confirmation_id"] = confirmation.confirmation_id
        result["response_step_status"] = "pending"
        result["task_run_status"] = "waiting_confirmation"
        result["task_run_stage"] = "requirement_clarification"
        return result

    def _recent_requirement_candidates(
        self,
        session_id: str,
        candidates: list[RequirementResolveCandidate],
        *,
        limit: int = 5,
    ) -> list[RequirementResolveCandidate]:
        merged = list(candidates)
        seen = {item.requirement_id for item in merged}
        if len(merged) >= limit:
            return merged[:limit]
        try:
            session_requirements = self.workflow.requirement_service.list_requirements(
                session_id=session_id,
                status="active",
                limit=limit,
            )
            global_requirements = self.workflow.requirement_service.list_requirements(status="active", limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load recent requirements for clarification: %s", exc)
            return merged[:limit]
        recent_requirements = list(session_requirements) + list(global_requirements)
        for item in recent_requirements:
            if item.requirement_id in seen:
                continue
            seen.add(item.requirement_id)
            merged.append(
                RequirementResolveCandidate(
                    requirement_id=item.requirement_id,
                    title=item.title,
                    summary=item.summary,
                    score=0.0,
                    reason="最近活跃需求，供用户确认归属。",
                )
            )
            if len(merged) >= limit:
                break
        return merged[:limit]

    def handle_mentioned_request(self, message: FeishuMessageContext, *, task_run_id: str | None = None) -> dict:
        workflow = self.workflow
        active_episode = workflow.memory_service.get_active_episode(message.session_id)
        active_episode_id = active_episode.id if active_episode else None
        base_workspace_context = workflow._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        requirement_workspace_context = workflow._requirement_workspace_context_for_task_run(task_run_id)
        if requirement_workspace_context:
            base_workspace_context = requirement_workspace_context
        workspace_context = base_workspace_context
        if task_run_id:
            workflow.task_run_service.upsert_step(
                task_run_id,
                step_key="workspace_context",
                title="构建协作上下文",
                step_type="context",
                status="done",
                output_payload={"context_length": len(base_workspace_context)},
            )

        graph_primary_result = None
        if workflow._should_run_graph_primary():
            graph_primary_result = workflow._run_graph_task_command(
                message,
                task_run_id=task_run_id,
                workspace_context=base_workspace_context,
                active_episode_id=active_episode_id,
                route_decision=None,
            )
        if graph_primary_result is not None:
            return graph_primary_result

        route_decision = workflow._route_request(message.text)
        if task_run_id:
            workflow.task_run_service.upsert_step(
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
            if route_decision.route != "unknown":
                self._merge_task_run_metadata(
                    task_run_id,
                    workflow._task_run_lifecycle_metadata(
                        route_decision.route,
                        list(route_decision.requested_outputs),
                    ),
                )
        logger.info(
            "Request route resolved: message_id=%s route=%s source=%s confidence=%.2f clarification=%s",
            message.message_id,
            route_decision.route,
            route_decision.source,
            route_decision.confidence,
            route_decision.needs_clarification,
        )
        workflow._run_graph_shadow(
            message,
            task_run_id=task_run_id,
            workspace_context=base_workspace_context,
            route_decision=route_decision,
        )
        if not workflow._should_run_graph_primary():
            graph_task_result = workflow._run_graph_task_command(
                message,
                task_run_id=task_run_id,
                workspace_context=base_workspace_context,
                active_episode_id=active_episode_id,
                route_decision=route_decision,
            )
            if graph_task_result is not None:
                return graph_task_result

        if route_decision.route not in {"doc", "slides", "canvas", "delivery"}:
            llm_task_intent_result = workflow.task_intent_execution.handle_llm_task_intent_instruction(
                message,
                route_decision=route_decision,
                workspace_context=base_workspace_context,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
            if llm_task_intent_result is not None:
                return llm_task_intent_result

            task_status_update_result = workflow.task_intent_execution.handle_task_status_update_instruction(
                message,
                route_decision=route_decision,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
            if task_status_update_result is not None:
                return task_status_update_result

            local_task_assignment_result = workflow.task_intent_execution.handle_local_task_assignment_instruction(
                message,
                route_decision=route_decision,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
            if local_task_assignment_result is not None:
                return local_task_assignment_result

        preplanned_llm_result: dict | None = None
        if workflow.llm_service.is_configured() and workflow._should_run_dag_planner(route_decision):
            try:
                preplanned_llm_result = workflow.llm_service.plan_workspace_request(
                    base_workspace_context,
                    message.text,
                )
                route_decision = workflow._route_decision_from_dag_result(
                    preplanned_llm_result,
                    fallback=route_decision,
                )
                if task_run_id:
                    workflow.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_dag_plan",
                        title="生成轻量 DAG 计划",
                        step_type="intent",
                        status="done",
                        output_payload={
                            "route": route_decision.route,
                            "confidence": route_decision.confidence,
                            "requested_outputs": list(route_decision.requested_outputs),
                            "plan_steps": [
                                item.get("type") or item.get("step_type")
                                for item in (
                                    preplanned_llm_result.get("plan", {}).get("steps", [])
                                    if isinstance(preplanned_llm_result.get("plan"), dict)
                                    else []
                                )
                                if isinstance(item, dict)
                            ],
                        },
                    )
                clarification = workflow.execution_planner.extract_clarification_request(preplanned_llm_result)
                if clarification and clarification["blocking"]:
                    return workflow._pause_for_clarification(
                        message,
                        intent=route_decision.route if route_decision.route != "unknown" else "help",
                        clarification=clarification,
                        active_episode_id=active_episode_id,
                        task_run_id=task_run_id,
                        workspace_context=workspace_context,
                        artifacts=[],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lightweight DAG planning failed, continuing with route fallback: %s", exc)
                preplanned_llm_result = None

        if route_decision.needs_clarification:
            return workflow._pause_for_clarification(
                message,
                intent=route_decision.route if route_decision.route != "unknown" else "help",
                clarification=workflow._clarification_from_route_decision(route_decision),
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[],
            )

        requirement_document = (
            workflow._requirement_document_target_for_task_run(task_run_id)
            if route_decision.route in {"doc", "slides", "canvas", "delivery"}
            else None
        )
        explicit_document = (
            workflow._resolve_target_document_for_instruction(message.session_id, message.text)
            if route_decision.route == "doc"
            else None
        )
        target_document = explicit_document if explicit_document is not None else requirement_document
        document_clarification = workflow._build_document_selection_clarification(
            message,
            route_decision,
            target_document=target_document,
        )
        if document_clarification:
            return workflow._pause_for_clarification(
                message,
                intent="doc",
                clarification=document_clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[],
            )

        if workflow.llm_service.is_configured() and workflow._should_run_memory_gate(route_decision, message.text):
            try:
                memory_gate = workflow.llm_service.should_recall_memories(base_workspace_context, message.text)
                if bool(memory_gate.get("should_recall")):
                    workspace_context = workflow._build_workspace_context_for_message(
                        message,
                        active_episode_id=active_episode_id,
                        include_semantic_search=True,
                    )
                    if task_run_id:
                        workflow.task_run_service.update_task_run(task_run_id, stage="semantic_recall")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory gate failed, continuing without semantic recall: %s", exc)

        if workflow.llm_service.is_configured():
            try:
                if task_run_id:
                    workflow.task_run_service.update_task_run(task_run_id, stage="intent_resolution")
                resolution_context = workflow._workspace_context_for_route(
                    route_decision,
                    message,
                    workspace_context,
                    active_episode_id=active_episode_id,
                    target_document=target_document,
                )
                llm_result = (
                    preplanned_llm_result
                    if preplanned_llm_result is not None
                    else workflow._resolve_llm_result_for_route(route_decision, resolution_context, message.text)
                )
                workflow._apply_route_decision_to_llm_result(llm_result, route_decision)
                protocol = workflow.execution_planner.normalize_request_protocol(llm_result)
                if task_run_id:
                    workflow.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_resolution",
                        title="识别请求意图",
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
                return workflow.execution_runner.execute_llm_request(
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
                    workflow.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_resolution",
                        title="识别请求意图",
                        step_type="intent",
                        status="failed",
                        error=str(exc),
                    )

        if task_run_id:
            workflow.task_run_service.update_task_run(task_run_id, stage="fallback")
        return workflow.fallback_handler.handle_fallback_request(message, active_episode_id, task_run_id=task_run_id)
