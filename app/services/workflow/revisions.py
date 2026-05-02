from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from app.services.tools.doc_tool import DocTool
from app.services.request_router import RouteDecision
from app.utils.values import coerce_positive_int

logger = logging.getLogger(__name__)


class WorkbenchRevisionWorkflow:
    """Runs workbench revision and confirmation-resume flows for FeishuWorkflowService."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def resume_task_run_after_confirmation(
        self,
        task_run_id: str,
        *,
        confirmation_id: str,
        answer_value: str,
        answered_by: str = "user",
    ) -> dict | None:
        workflow = self.workflow
        detail = workflow.task_run_service.get_task_run(task_run_id)
        if detail is None:
            return None

        metadata = workflow.task_run_service.get_task_run_metadata(task_run_id)
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

        resumed_instruction = build_confirmation_resume_instruction(instruction, answer_value)
        intent = str(resume_payload.get("intent") or "status")
        target_document = None
        if intent == "doc":
            target_document = workflow._resolve_target_document_for_instruction(detail.session_id, resumed_instruction)
            if target_document is not None:
                workspace_context = workflow._join_context_blocks(
                    workspace_context,
                    DocTool.format_current_document_context(target_document),
                )
        workflow.task_run_service.upsert_step(
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
        workflow.task_run_service.update_task_run(task_run_id, stage="confirmation_replanning", status="running")

        metadata.pop("resume_after_confirmation", None)
        metadata["last_confirmation"] = {
            "confirmation_id": confirmation_id,
            "answer_value": answer_value,
            "answered_by": answered_by,
        }
        workflow.task_run_service.update_task_run(task_run_id, metadata=metadata)

        if not workflow.llm_service.is_configured():
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
            workflow.result_persistence.persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
            return result

        route_decision = RouteDecision(route=intent, source="confirmation", confidence=1.0)
        llm_result = workflow._resolve_llm_result_for_route(route_decision, workspace_context, resumed_instruction)
        workflow._apply_route_decision_to_llm_result(llm_result, route_decision)
        synthetic_message = SimpleNamespace(
            session_id=detail.session_id,
            message_id=detail.trigger_message_id or confirmation_id,
            text=resumed_instruction,
            chat_id=None,
        )
        result = workflow.execution_runner.execute_llm_request(
            synthetic_message,
            llm_result,
            workspace_context,
            active_episode_id,
            task_run_id=task_run_id,
            target_document=target_document,
        )
        workflow.result_persistence.persist_task_run_result(task_run_id, message_text=resumed_instruction, result=result, session_id=detail.session_id)
        return result

    def revise_document_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        document_id: str | None = None,
    ):
        workflow = self.workflow
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Document revision instruction cannot be empty.")

        source_detail = workflow.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        session_id = source_detail.session_id
        revision_start = workflow._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=workflow._task_run_title(cleaned_instruction, "doc_revision"),
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
        workflow._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="document",
        )
        workspace_context = workflow._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="document",
        )
        try:
            requested_document_id = (document_id or "").strip()
            if requested_document_id:
                current_doc = workflow.session_document_service.get_document(session_id, requested_document_id)
                if current_doc is None:
                    raise ValueError(f"Document not found in session history: {requested_document_id}")
            else:
                current_doc = workflow.session_document_service.get_current_document(session_id)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ValueError):
                raise
            logger.warning("Failed to load current document context for revision: %s", exc)
            current_doc = None
        workspace_context = workflow._join_context_blocks(
            workspace_context,
            DocTool.format_current_document_context(current_doc),
        )
        workflow.task_run_service.upsert_step(
            task_run.task_run_id,
            step_key="workspace_context",
            title="构建文档修订上下文",
            step_type="context",
            status="done",
            output_payload={"context_length": len(workspace_context)},
        )

        llm_result: dict = {}
        if workflow.llm_service.is_configured():
            try:
                workflow.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_planning")
                revision_instruction = DocTool.build_document_revision_instruction(
                    cleaned_instruction,
                    current_doc=current_doc,
                )
                try:
                    llm_result = workflow.llm_service.resolve_doc_request(workspace_context, revision_instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Specialized doc revision request failed, falling back to workspace resolver: %s",
                        exc,
                    )
                    llm_result = workflow.llm_service.resolve_workspace_request(
                        workspace_context,
                        revision_instruction,
                    )
                llm_result["operation"] = "update"
                llm_result["object"] = "doc"
                llm_result["route"] = "doc"
                llm_result["intent"] = "doc"
                workflow.task_run_service.upsert_step(
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
                workflow.task_run_service.upsert_step(
                    task_run.task_run_id,
                    step_key="intent_resolution",
                    title="识别文档修订目标",
                    step_type="intent",
                    status="failed",
                    error=str(exc),
                )

        workflow.task_run_service.update_task_run(task_run.task_run_id, stage="doc_revision_processing")
        synthetic_message = SimpleNamespace(
            session_id=session_id,
            message_id=synthetic_message_id,
            text=cleaned_instruction,
            chat_id=None,
        )
        try:
            result = workflow.doc_execution.prepare_doc_execution(
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
            workflow.result_persistence.persist_task_run_result(
                task_run.task_run_id,
                message_text=cleaned_instruction,
                result=result,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.task_run_service.update_task_run(
                task_run.task_run_id,
                stage="failed",
                status="failed",
                latest_error=str(exc),
            )
            workflow.task_run_service.upsert_step(
                task_run.task_run_id,
                step_key="response_generated",
                title="生成文档修订结果",
                step_type="workflow",
                status="failed",
                error=str(exc),
            )
            raise

        return workflow.task_run_service.get_task_run(task_run.task_run_id)

    def revise_slides_from_task_run(
        self,
        source_task_run_id: str,
        *,
        instruction: str,
        requested_by: str = "pilot_workbench",
        artifact_id: str | None = None,
    ):
        workflow = self.workflow
        cleaned_instruction = " ".join((instruction or "").split()).strip()
        if not cleaned_instruction:
            raise ValueError("Slides revision instruction cannot be empty.")

        source_detail = workflow.task_run_service.get_task_run(source_task_run_id)
        if source_detail is None:
            return None

        presentation_tool = workflow._presentation_tool()
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
        revision_start = workflow._workbench_revision_tool().start_run(
            session_id=session_id,
            source_task_run_id=source_task_run_id,
            title=workflow._task_run_title(cleaned_instruction, "slides_revision"),
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
        workflow._workbench_revision_tool().remember_user_instruction(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            requested_by=requested_by,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = workflow._workbench_revision_tool().build_workspace_context(
            session_id=session_id,
            synthetic_message_id=synthetic_message_id,
            instruction=cleaned_instruction,
            label="slides",
        )
        workspace_context = workflow._join_context_blocks(
            workspace_context,
            "[当前演示稿包]\n" + json.dumps(current_package, ensure_ascii=False)[:12000],
        )
        workflow.task_run_service.upsert_step(
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
        workflow.task_run_service.update_task_run(task_run.task_run_id, stage="slides_revision_processing")
        if workflow.llm_service.is_configured():
            try:
                revised_package = workflow.llm_service.revise_presentation_package(
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
                workflow.task_run_service.upsert_step(
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
                workflow.task_run_service.upsert_step(
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
            workflow.task_run_service.update_task_run(task_run.task_run_id, status="failed", error=error)
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
        reply_preview = "【演示稿修订】\n" + presentation_tool.format_reply(revised_package, artifact=artifact)
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
        workflow.result_persistence.persist_task_run_result(
            task_run.task_run_id,
            message_text=cleaned_instruction,
            result=result,
            session_id=session_id,
        )
        return workflow.task_run_service.get_task_run(task_run.task_run_id)


def build_confirmation_resume_instruction(instruction: str, answer_value: str) -> str:
    base = instruction.strip() or "继续刚才的任务"
    return f"{base}\n\n[用户刚刚确认]\n{answer_value}"
