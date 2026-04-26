import json
import logging
import re
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.doc_api import FeishuDocAPI
from app.feishu.message_api import FeishuMessageAPI
from app.feishu.user_api import FeishuUserAPI
from app.schemas.analyze import AgentTrace, AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.interaction import InteractionService
from app.services.llm import LLMService
from app.services.memory_service import MemoryService
from app.services.task_run_service import TaskRunService
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
        self.doc_api = FeishuDocAPI()
        self.user_api = FeishuUserAPI()
        self.memory_service = MemoryService()
        self.task_run_service = TaskRunService()
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()

    def handle_message(self, message: FeishuMessageContext) -> dict:
        started_at = time.perf_counter()
        active_episode_id: int | None = None
        if message.chat_type == "group" and not message.is_mentioned:
            active_episode = self.memory_service.ensure_active_episode(message.session_id)
            active_episode_id = active_episode.id

        self._ensure_sender_alias(message)

        self.memory_service.save_user_message(
            session_id=message.session_id,
            message_id=message.message_id,
            sender_id=message.sender_id,
            content=message.text or message.raw_text,
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
        for artifact in result.get("artifacts", []):
            self.task_run_service.create_artifact(
                task_run.task_run_id,
                artifact_type=str(artifact.get("artifact_type") or "note"),
                title=str(artifact.get("title") or "协作产物"),
                provider=str(artifact.get("provider") or "local"),
                status=str(artifact.get("status") or "ready"),
                url=str(artifact.get("url") or "").strip() or None,
                preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
            )
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
        base_workspace_context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            include_semantic_search=False,
            episode_id=active_episode_id,
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

        if self.llm_service.is_configured():
            try:
                memory_gate = self.llm_service.should_recall_memories(base_workspace_context, message.text)
                if bool(memory_gate.get("should_recall")):
                    workspace_context = self.memory_service.build_workspace_context(
                        message.session_id,
                        include_pending=True,
                        exclude_message_id=message.message_id,
                        query_text=message.text,
                        include_semantic_search=True,
                        episode_id=active_episode_id,
                    )
                    if task_run_id:
                        self.task_run_service.update_task_run(task_run_id, stage="semantic_recall")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory gate failed, continuing without semantic recall: %s", exc)

        if self.llm_service.is_configured():
            try:
                if task_run_id:
                    self.task_run_service.update_task_run(task_run_id, stage="intent_resolution")
                llm_result = self.llm_service.resolve_workspace_request(workspace_context, message.text)
                if task_run_id:
                    self.task_run_service.upsert_step(
                        task_run_id,
                        step_key="intent_resolution",
                        title="识别任务意图",
                        step_type="intent",
                        status="done",
                        output_payload={
                            "intent": llm_result.get("intent"),
                            "reason": llm_result.get("reason"),
                        },
                    )
                return self._execute_llm_request(
                    message,
                    llm_result,
                    workspace_context,
                    active_episode_id,
                    task_run_id=task_run_id,
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

    def _execute_llm_request(
        self,
        message: FeishuMessageContext,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        intent = str(llm_result.get("intent") or "").strip().lower()
        reason = str(llm_result.get("reason") or "").strip()
        if task_run_id:
            self.task_run_service.update_task_run(
                task_run_id,
                intent=intent or None,
                title=self._task_run_title(message.text, intent or None),
                stage=f"{intent or 'help'}_processing",
            )

        plan_artifact = self._build_plan_artifact(intent=intent, reason=reason, llm_result=llm_result)
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

        if intent in {"help", "unknown", ""}:
            return self._deliver_reply(
                message,
                "help",
                self._format_help_reply(reason),
                analysis=None,
                artifacts=[plan_artifact] if plan_artifact else None,
            )

        if intent == "slides":
            package = llm_result.get("slides")
            if not isinstance(package, dict) or not package.get("slides"):
                try:
                    package = self.llm_service.generate_presentation_package(workspace_context, message.text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Slide package fallback generation failed: %s", exc)
                    package = self._build_fallback_presentation_package(message.session_id)
            reply_preview = self._format_presentation_reply(package)
            result = self._deliver_reply(
                message,
                "slides",
                reply_preview,
                analysis=None,
                episode_id=active_episode_id,
                artifacts=self._append_artifacts(
                    [plan_artifact] if plan_artifact else None,
                    {
                        "artifact_type": "slides_package",
                        "provider": "llm",
                        "title": str(package.get("theme") or "演示稿"),
                        "preview": package,
                    },
                ),
            )
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title="slides")
            return result

        if intent == "doc":
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
            sync_lines = self._sync_package_to_doc(package)
            reply_preview = self._format_doc_reply(package, sync_lines)
            result = self._deliver_reply(
                message,
                "doc",
                reply_preview,
                analysis=analysis,
                episode_id=active_episode_id,
                artifacts=self._append_artifacts(
                    [plan_artifact] if plan_artifact else None,
                    {
                        "artifact_type": "document",
                        "provider": "feishu_doc" if any("文档链接" in line for line in sync_lines) else "local",
                        "title": str(package.get("title") or "协同文档"),
                        "url": self._extract_first_url(sync_lines),
                        "preview": package,
                    },
                ),
            )
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title=str(package.get("title") or "doc"))
            return result

        if intent == "status":
            status_answer = str(llm_result.get("status_answer") or "").strip()
            if not status_answer:
                tasks = self.memory_service.get_current_tasks(message.session_id)
                payload = self.memory_service.load_memory_payload(message.session_id)
                status_answer = self._format_status_reply(message.text, tasks, payload)
            return self._deliver_reply(
                message,
                "status",
                status_answer,
                analysis=None,
                artifacts=[plan_artifact] if plan_artifact else None,
            )

        if intent in {"summary", "tasks", "risks"}:
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
                reason=reason,
            )
            reply_preview = self._format_analysis_reply(analysis, intent)
            self.memory_service.save_round(
                session_id=message.session_id,
                analysis=analysis,
                episode_id=active_episode_id,
                async_embed=True,
                preserve_unmatched_previous=False,
            )
            result = self._deliver_reply(
                message,
                intent,
                reply_preview,
                analysis=analysis,
                episode_id=active_episode_id,
                artifacts=[plan_artifact] if plan_artifact else None,
            )
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title=analysis.summary)
            return result

        return self._deliver_reply(
            message,
            "help",
            self._format_help_reply(reason),
            analysis=None,
            artifacts=[plan_artifact] if plan_artifact else None,
        )

    def _extract_clarification_request(self, llm_result: dict) -> dict | None:
        raw = llm_result.get("clarification")
        if not isinstance(raw, dict) or not bool(raw.get("needed")):
            return None

        question = str(raw.get("question") or "").strip()
        if not question:
            return None

        options = (
            [str(item).strip() for item in raw.get("options", []) if str(item).strip()][:4]
            if isinstance(raw.get("options"), list)
            else []
        )
        reason = str(raw.get("reason") or "").strip()
        blocking = raw.get("blocking")
        return {
            "question": question,
            "reason": reason,
            "options": options,
            "blocking": True if blocking is None else bool(blocking),
        }

    def _build_plan_artifact(self, *, intent: str, reason: str, llm_result: dict) -> dict | None:
        next_actions = (
            [str(item).strip() for item in llm_result.get("next_actions", []) if str(item).strip()]
            if isinstance(llm_result.get("next_actions"), list)
            else []
        )
        clarification = self._extract_clarification_request(llm_result)
        preview = {
            "intent": intent or "unknown",
            "reason": reason,
            "next_actions": next_actions[:4],
            "task_operation_count": len(llm_result.get("task_operations", []))
            if isinstance(llm_result.get("task_operations"), list)
            else 0,
            "risk_count": len(llm_result.get("risks", []))
            if isinstance(llm_result.get("risks"), list)
            else 0,
        }
        if clarification:
            preview["clarification"] = clarification

        if not any(preview.values()):
            return None

        return {
            "artifact_type": "agent_plan",
            "provider": "llm",
            "title": "Agent 执行规划",
            "status": "needs_confirmation" if clarification and clarification["blocking"] else "ready",
            "preview": preview,
        }

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
        label = {
            "doc": "文档",
            "slides": "演示稿",
            "summary": "讨论总结",
            "tasks": "任务整理",
            "risks": "风险判断",
            "status": "状态回答",
        }.get(intent, "协作处理")
        lines = [f"【Agent 需要再确认一下】({label})", clarification["question"]]
        reason = str(clarification.get("reason") or "").strip()
        if reason:
            lines.append(f"原因：{reason}")
        options = clarification.get("options") or []
        if options:
            lines.append("可选方案：")
            for index, option in enumerate(options, start=1):
                lines.append(f"{index}. {option}")
        lines.append("你可以在工作台里直接确认，或继续回复我更具体的要求。")
        return "\n".join(lines)

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
                "mode": str(resume_payload.get("intent") or "status"),
                "analysis": None,
                "reply_preview": f"已记录确认：{answer_value}。当前未配置可继续自动执行的 LLM，请稍后重新发起一次请求。",
                "reply_sent": False,
                "reply_error": None,
                "artifacts": [],
            }
            self._persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
            return result

        llm_result = self.llm_service.resolve_workspace_request(workspace_context, resumed_instruction)
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
        )
        self._persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
        return result

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
        for artifact in result.get("artifacts", []):
            self.task_run_service.create_artifact(
                task_run_id,
                artifact_type=str(artifact.get("artifact_type") or "note"),
                title=str(artifact.get("title") or "协作产物"),
                provider=str(artifact.get("provider") or "local"),
                status=str(artifact.get("status") or "ready"),
                url=str(artifact.get("url") or "").strip() or None,
                preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
            )
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
        refreshed = [task.model_copy(deep=True) for task in current_tasks]
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "").strip().lower()
            match_hint = operation.get("match_hint") if isinstance(operation.get("match_hint"), dict) else {}
            task_payload = operation.get("task") if isinstance(operation.get("task"), dict) else None

            if action == "create" and task_payload:
                refreshed.append(TaskItem.model_validate(task_payload))
                continue

            target_index = self._find_operation_target(refreshed, match_hint, task_payload)
            if target_index is None:
                if action == "create" and task_payload:
                    refreshed.append(TaskItem.model_validate(task_payload))
                continue

            if action == "remove":
                refreshed.pop(target_index)
                continue

            if action == "update" and task_payload:
                refreshed[target_index] = TaskItem.model_validate(task_payload)

        return refreshed

    def _merge_task_items(self, current_tasks: list[TaskItem], refreshed_tasks: list[TaskItem]) -> list[TaskItem]:
        if not current_tasks:
            return [task.model_copy(deep=True) for task in refreshed_tasks]

        merged = [task.model_copy(deep=True) for task in current_tasks]
        for task in refreshed_tasks:
            target_index = self._find_merge_target(merged, task)
            if target_index is None:
                merged.append(task.model_copy(deep=True))
                continue
            merged[target_index] = task.model_copy(deep=True)
        return merged

    def _update_current_tasks_from_discussion(
        self,
        current_tasks: list[TaskItem],
        source_text: str,
        llm_tasks: list[TaskItem],
    ) -> list[TaskItem]:
        updated = apply_discussion_updates(current_tasks, source_text)
        explicit_tasks = normalize_tasks(extract_tasks(source_text))
        if explicit_tasks:
            return self._merge_task_items(updated, explicit_tasks)
        if llm_tasks:
            return self._merge_task_items(updated, llm_tasks)
        return updated

    def _find_merge_target(self, tasks: list[TaskItem], incoming_task: TaskItem) -> int | None:
        incoming_title = " ".join((incoming_task.title or "").lower().split())
        incoming_owner = " ".join((incoming_task.owner or "").lower().split())

        exact_matches = [
            idx
            for idx, task in enumerate(tasks)
            if " ".join((task.title or "").lower().split()) == incoming_title
            and " ".join((task.owner or "").lower().split()) == incoming_owner
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]

        owner_missing = incoming_owner in {"", "tbd"}
        if owner_missing:
            title_matches = [
                idx
                for idx, task in enumerate(tasks)
                if " ".join((task.title or "").lower().split()) == incoming_title
            ]
            if len(title_matches) == 1:
                return title_matches[0]

        return None

    def _find_operation_target(
        self,
        tasks: list[TaskItem],
        match_hint: dict,
        task_payload: dict | None,
    ) -> int | None:
        hint_title = str(match_hint.get("title") or "").strip()
        hint_owner = str(match_hint.get("owner") or "").strip()
        payload_title = str(task_payload.get("title") or "").strip() if task_payload else ""
        payload_owner = str(task_payload.get("owner") or "").strip() if task_payload else ""

        def normalized(value: str) -> str:
            return " ".join(value.lower().split())

        candidates: list[tuple[str, str]] = []
        if hint_title or hint_owner:
            candidates.append((hint_title, hint_owner))
        if payload_title or payload_owner:
            candidates.append((payload_title, payload_owner))

        for title, owner in candidates:
            exact_matches = [
                idx
                for idx, task in enumerate(tasks)
                if (not title or normalized(task.title) == normalized(title))
                and (not owner or normalized(task.owner) == normalized(owner))
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]

        for title, _ in candidates:
            if not title:
                continue
            title_matches = [idx for idx, task in enumerate(tasks) if normalized(task.title) == normalized(title)]
            if len(title_matches) == 1:
                return title_matches[0]

        return None

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

        if decision.mode == "status":
            tasks = self.memory_service.get_current_tasks(message.session_id)
            payload = self.memory_service.load_memory_payload(message.session_id)
            reply_preview = self._format_status_reply(message.text, tasks, payload)
            return self._deliver_reply(message, "status", reply_preview, analysis=None)

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
        workspace_context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            episode_id=active_episode_id,
        )
        if not workspace_context.strip():
            reply = "我这边还没有拿到可用的讨论素材。先在群里把目标、分工和结论聊出来，再让我生成汇报大纲会更准确。"
            return self._deliver_reply(message, "slides", reply, analysis=None)

        try:
            package = self.llm_service.generate_presentation_package(workspace_context, message.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            package = self._build_fallback_presentation_package(message.session_id)

        reply_preview = self._format_presentation_reply(package)
        result = self._deliver_reply(
            message,
            "slides",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=[
                {
                    "artifact_type": "slides_package",
                    "provider": "fallback",
                    "title": str(package.get("theme") or "演示稿"),
                    "preview": package,
                }
            ],
        )
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title="slides")
        return result

    def _handle_fallback_doc(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workspace_context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            episode_id=active_episode_id,
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
        sync_lines = self._sync_package_to_doc(package)
        reply_preview = self._format_doc_reply(package, sync_lines)
        result = self._deliver_reply(
            message,
            "doc",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
            artifacts=[
                {
                    "artifact_type": "document",
                    "provider": "feishu_doc" if any("文档链接" in line for line in sync_lines) else "fallback",
                    "title": str(package.get("title") or "协同文档"),
                    "url": self._extract_first_url(sync_lines),
                    "preview": package,
                }
            ],
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
        provided = llm_result.get("doc")
        if isinstance(provided, dict) and isinstance(provided.get("sections"), list) and provided.get("sections"):
            base_title = str(provided.get("title") or "").strip() or self._default_doc_title(instruction, stats_as_of=stats_as_of)
            return {
                "title": self._compose_doc_title(base_title, stats_as_of=stats_as_of),
                "stats_as_of": stats_as_of,
                "sections": provided.get("sections", []),
            }

        wants_outline = any(keyword in instruction for keyword in ("汇报", "路演", "大纲", "PPT", "ppt", "演示"))
        if wants_outline:
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
        if self._is_outline_request(instruction):
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

    def _is_outline_request(self, instruction: str) -> bool:
        return any(keyword in instruction for keyword in ("汇报", "路演", "大纲", "PPT", "ppt", "演示"))

    def _document_from_analysis(self, analysis: AnalyzeResponse, instruction: str, *, stats_as_of: str | None = None) -> dict:
        sections = [
            {
                "heading": "讨论摘要",
                "paragraphs": [analysis.summary],
            }
        ]
        if analysis.tasks:
            sections.append(
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        f"{idx}. {task.title}｜负责人：{task.owner}｜截止：{task.due_date}｜优先级：{task.priority}"
                        for idx, task in enumerate(analysis.tasks, start=1)
                    ],
                }
            )
        if analysis.risks:
            sections.append(
                {
                    "heading": "风险与卡点",
                    "paragraphs": [f"{idx}. {risk}" for idx, risk in enumerate(analysis.risks, start=1)],
                }
            )
        if analysis.next_actions:
            sections.append(
                {
                    "heading": "下一步建议",
                    "paragraphs": [f"{idx}. {item}" for idx, item in enumerate(analysis.next_actions, start=1)],
                }
            )
        return {
            "title": self._default_doc_title(instruction, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": sections,
        }

    def _document_from_presentation(self, package: dict, instruction: str, *, stats_as_of: str | None = None) -> dict:
        theme = str(package.get("theme") or "汇报大纲").strip()
        audience = str(package.get("audience") or "团队协作汇报").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        sections = [
            {
                "heading": "文档说明",
                "paragraphs": [f"主题：{theme}", f"适用场景：{audience}"],
            }
        ]
        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"P{index}").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            sections.append(
                {
                    "heading": f"P{index}. {title}",
                    "paragraphs": [str(item).strip() for item in bullets if str(item).strip()],
                }
            )
        if emphasis:
            sections.append(
                {
                    "heading": "演示重点",
                    "paragraphs": [str(item).strip() for item in emphasis if str(item).strip()],
                }
            )
        if assets:
            sections.append(
                {
                    "heading": "建议补充素材",
                    "paragraphs": [str(item).strip() for item in assets if str(item).strip()],
                }
            )
        return {
            "title": self._default_doc_title(instruction, fallback=theme, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": sections,
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
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        lines = ["【文档同步】", f"标题：{str(package.get('title') or '协同文档').strip()}"]
        if sections:
            lines.append("正文结构：")
            for index, section in enumerate(sections[:6], start=1):
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("heading") or f"部分 {index}").strip()
                paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
                lines.append(f"{index}. {heading}")
                for paragraph in paragraphs[:2]:
                    content = str(paragraph).strip()
                    if content:
                        lines.append(f"- {content}")
        lines.append("同步结果：")
        lines.extend(sync_lines)
        return "\n".join(lines)

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
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    def _resolve_doc_stats_as_of(self, session_id: str, *, episode_id: int | None) -> str | None:
        cutoff_at = self.memory_service.get_discussion_cutoff_at(session_id, episode_id=episode_id)
        if cutoff_at is None:
            return None
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=ZoneInfo("UTC"))
        return cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    def _compose_doc_title(self, base_title: str, *, stats_as_of: str | None) -> str:
        title = base_title.strip()
        if not title:
            title = settings.feishu_doc_title_prefix
        if "统计至" in title:
            return title
        if stats_as_of:
            return f"{title} - 统计至{stats_as_of}"
        return title

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
        header = {
            "summary": "【讨论总结】",
            "tasks": "【待办清单】",
            "risks": "【风险与卡点】",
        }.get(mode, "【协作整理】")

        lines = [header, f"摘要：{analysis.summary}"]

        if mode in {"summary", "tasks"}:
            lines.append("任务：")
            if analysis.tasks:
                for idx, task in enumerate(analysis.tasks, start=1):
                    lines.append(
                        f"{idx}. {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 优先级：{task.priority}"
                    )
            else:
                lines.append("1. 当前讨论还没有形成明确待办。")

        if mode in {"summary", "risks"}:
            lines.append("风险：")
            if analysis.risks:
                for idx, risk in enumerate(analysis.risks, start=1):
                    lines.append(f"{idx}. {risk}")
            else:
                lines.append("1. 当前没有识别到新的显性风险。")

        lines.append("下一步建议：")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"{idx}. {action}")

        return "\n".join(lines)

    def _format_status_reply(self, query: str, tasks: list, payload: dict) -> str:
        if not tasks:
            return "我这边还没有现成的任务快照。你可以先让我总结一下或整理待办，我再基于结果回答状态问题。"

        if "没负责人" in query or "未分配" in query:
            pending = [task for task in tasks if task.owner == "TBD"]
            if not pending:
                return "【负责人检查】\n当前任务都已经有明确负责人，没有未分配项。"
            lines = ["【负责人检查】", "以下任务还没有明确负责人："]
            for idx, task in enumerate(pending, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        if "谁负责" in query:
            lines = ["【当前分工】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} -> {task.owner}")
            return "\n".join(lines)

        if "截止" in query or "到期" in query:
            lines = ["【时间节点】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        if "风险" in query or "卡点" in query or "阻塞" in query:
            risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
            if not risks:
                risks = ["当前没有额外记录到新的风险项，但仍建议确认负责人和截止时间。"]
            lines = ["【当前风险】"]
            for idx, risk in enumerate(risks, start=1):
                lines.append(f"{idx}. {risk}")
            return "\n".join(lines)

        completed = sum(1 for task in tasks if str(task.status).lower() == "done")
        unassigned = sum(1 for task in tasks if task.owner == "TBD")
        return "\n".join(
            [
                "【当前协作状态】",
                f"- 任务总数：{len(tasks)}",
                f"- 已完成：{completed}",
                f"- 待确认负责人：{unassigned}",
                "- 如需更具体输出，可以继续问：谁负责什么 / 哪些任务没负责人 / 当前有什么风险。",
            ]
        )

    def _format_presentation_reply(self, package: dict) -> str:
        theme = str(package.get("theme") or "基于群聊讨论的协作汇报").strip()
        audience = str(package.get("audience") or "项目汇报 / 路演准备").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        lines = ["【汇报大纲】", f"主题：{theme}", f"适用场景：{audience}"]

        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"第{index}页").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            lines.append(f"P{index}. {title}")
            for bullet in bullets[:4]:
                lines.append(f"- {str(bullet).strip()}")

        if emphasis:
            lines.append("演示时重点强调：")
            for item in emphasis[:3]:
                lines.append(f"- {str(item).strip()}")

        if assets:
            lines.append("建议补充素材：")
            for item in assets[:4]:
                lines.append(f"- {str(item).strip()}")

        return "\n".join(lines)

    def _build_fallback_presentation_package(self, session_id: str) -> dict:
        tasks = self.memory_service.get_current_tasks(session_id)
        payload = self.memory_service.load_memory_payload(session_id)

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
        lines = ["【我可以这样帮你】"]
        if reason:
            lines.append(f"提示：{reason}")
        lines.extend(
            [
                "- @我 总结一下这次讨论",
                "- @我 帮我整理待办",
                "- @我 看一下当前风险和卡点",
                "- @我 现在还有哪些任务没负责人",
                "- @我 帮我搞个汇报大纲",
            ]
        )
        return "\n".join(lines)
