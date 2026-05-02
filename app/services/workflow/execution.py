from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.planner import ExecutionPlan, PlannerStep

logger = logging.getLogger(__name__)


class WorkflowExecutionRunner:
    """Runs planned workflow steps while FeishuWorkflowService owns concrete tools."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def execute_llm_request(
        self,
        message: FeishuMessageContext,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
        target_document: dict | None = None
    ) -> dict:
        workflow = self.workflow
        protocol = workflow.execution_planner.normalize_request_protocol(llm_result)
        protocol = workflow.execution_planner.adjust_protocol_for_instruction(
            protocol,
            instruction=message.text,
            llm_result=llm_result,
        )
        intent = protocol.route
        reason = str(llm_result.get("reason") or "").strip()
        if task_run_id:
            workflow.task_run_service.update_task_run(
                task_run_id,
                intent=intent or None,
                title=workflow._task_run_title(message.text, intent or None),
                stage=f"{intent or 'help'}_processing",
            )

        plan = workflow.execution_planner.resolve_execution_plan(
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
        plan_artifact = workflow.execution_planner.build_plan_artifact(plan=plan, reason=reason, llm_result=llm_result)
        if task_run_id:
            workflow.task_run_service.upsert_step(
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
        clarification = workflow.execution_planner.extract_clarification_request(llm_result)
        if clarification and clarification["blocking"]:
            return workflow._pause_for_clarification(
                message,
                intent=intent,
                clarification=clarification,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
                workspace_context=workspace_context,
                artifacts=[plan_artifact] if plan_artifact else None,
            )

        return self.execute_plan(
            message,
            plan=plan,
            llm_result=llm_result,
            workspace_context=workspace_context,
            active_episode_id=active_episode_id,
            task_run_id=task_run_id,
            base_artifacts=[plan_artifact] if plan_artifact else None,
            target_document=target_document,
        )

    def execute_plan(
        self,
        message: FeishuMessageContext,
        *,
        plan: ExecutionPlan,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
        base_artifacts: list[dict] | None = None,
        target_document: dict | None = None
    ) -> dict:
        workflow = self.workflow
        plan = workflow.execution_planner.schedule_execution_plan(plan)
        combined_artifacts = list(base_artifacts or [])
        reply_parts: list[str] = []
        final_analysis: AnalyzeResponse | None = None
        close_title: str | None = None

        if not plan.steps:
            fallback_reply = workflow.response_formatter.format_help_reply("当前还没有可执行的计划步骤。")
            return workflow.reply_sender.deliver_reply(
                message,
                plan.primary_intent or "help",
                fallback_reply,
                analysis=None,
                episode_id=active_episode_id,
                artifacts=combined_artifacts,
            )

        for index, step in enumerate(plan.steps, start=1):
            if task_run_id:
                workflow.task_run_service.update_task_run(
                    task_run_id,
                    stage=f"executing_{step.step_type}",
                    status="running",
                )
                workflow.task_run_service.upsert_step(
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
                step_result = self.execute_plan_step(
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
                    workflow.task_run_service.upsert_step(
                        task_run_id,
                        step_key=f"plan_{index}_{step.step_type}",
                        title=step.title,
                        step_type="plan_execution",
                        status="failed",
                        error=str(exc),
                    )
                raise

            if task_run_id:
                workflow.task_run_service.upsert_step(
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

        final_reply = workflow.response_formatter.combine_plan_replies(reply_parts)
        result = workflow.reply_sender.deliver_reply(
            message,
            plan.primary_intent or "help",
            final_reply,
            analysis=final_analysis,
            episode_id=active_episode_id,
            artifacts=combined_artifacts,
        )
        if active_episode_id is not None and workflow._should_close_episode(result, final_reply):
            workflow.memory_service.close_active_episode(
                message.session_id,
                title=close_title or (final_analysis.summary if final_analysis is not None else plan.primary_intent),
            )
        return result

    def execute_plan_step(
        self,
        message: FeishuMessageContext,
        *,
        step: PlannerStep,
        plan: ExecutionPlan,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        task_run_id: str | None,
        target_document: dict | None = None
    ) -> dict:
        workflow = self.workflow
        if step.step_type == "generate_slides":
            return workflow.slides_execution.prepare_slides_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=task_run_id,
            )
        if step.step_type == "generate_canvas":
            return workflow.canvas_execution.prepare_canvas_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=task_run_id,
            )
        if step.step_type == "sync_doc":
            return workflow.doc_execution.prepare_doc_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=active_episode_id,
                reason=plan.goal,
                task_run_id=task_run_id,
                target_document=target_document,
            )
        if step.step_type == "answer_status":
            return workflow.status_execution.prepare_status_execution(
                message,
                llm_result=llm_result,
                active_episode_id=active_episode_id,
                task_run_id=task_run_id,
            )
        if step.step_type == "analyze_discussion":
            return workflow.analysis_execution.prepare_analysis_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=active_episode_id,
                intent=plan.primary_intent,
            )
        return {
            "reply_preview": workflow.response_formatter.format_help_reply(
                str(llm_result.get("reason") or "").strip()
            ),
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }
