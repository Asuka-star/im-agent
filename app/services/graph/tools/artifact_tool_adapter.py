from __future__ import annotations

import json
from typing import Any

from app.schemas.feishu_event import FeishuMessageContext
from app.services.graph.state import PlanStep, WorkerResult, WorkflowGraphState, WorkspaceCommand
from app.utils.values import coerce_positive_int


class ArtifactWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        command = state.command or WorkspaceCommand()
        if step.worker == "slides" and command.operation in {"revise", "update"}:
            return self._revise_slides(state, step, command)
        if step.worker == "canvas" and command.operation in {"revise", "update"}:
            return self._revise_canvas(state, step, command)
        llm_result = self._llm_result_for_step(command, step)
        message = _message_context(state)
        workspace_context = state.context.excerpt if state.context else ""
        if step.worker == "doc":
            result = self.workflow.doc_execution.prepare_doc_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                active_episode_id=state.active_episode_id,
                reason=command.reason,
                task_run_id=state.task_run_id,
                target_document=self._target_document_for_step(state, command),
            )
        elif step.worker == "slides":
            result = self.workflow.slides_execution.prepare_slides_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=state.task_run_id,
            )
        elif step.worker == "canvas":
            result = self.workflow.canvas_execution.prepare_canvas_execution(
                message,
                llm_result=llm_result,
                workspace_context=workspace_context,
                task_run_id=state.task_run_id,
            )
        else:
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=False,
                status="failed",
                error=f"unsupported artifact worker: {step.worker}",
            )
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": step.worker,
                "reply_preview": result.get("reply_preview"),
                "analysis": result.get("analysis").model_dump(mode="json")
                if hasattr(result.get("analysis"), "model_dump")
                else result.get("analysis"),
                "artifacts": result.get("artifacts", []),
                "close_title": result.get("close_title"),
            },
        )

    def _revise_slides(self, state: WorkflowGraphState, step: PlanStep, command: WorkspaceCommand) -> WorkerResult:
        artifact = _latest_artifact(state, {"slides_package", "slides"})
        package = _artifact_preview(artifact)
        if not artifact or not isinstance(package.get("slides"), list) or not package.get("slides"):
            return _needs_review(
                step,
                "Slides revision requires an existing slides artifact.",
                "当前还没有可修订的 PPT 产物。要先生成一份汇报稿吗？",
                ["先生成 PPT", "重新说明要修订的产物"],
            )
        tool = self.workflow._presentation_tool()
        instruction = state.message.text
        workspace_context = state.context.excerpt if state.context else ""
        raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
        edit_plan = tool.plan_revision(package, instruction, raw_payload)
        provider = "local"
        revised = None
        llm_service = getattr(self.workflow, "llm_service", None)
        if llm_service is not None and getattr(llm_service, "is_configured", lambda: False)():
            try:
                revised = llm_service.revise_presentation_package(package, workspace_context, instruction)
                edit_plan = tool.plan_revision(package, instruction, revised)
                provider = "llm"
            except Exception:
                revised = None
                provider = "fallback"
        if not isinstance(revised, dict):
            revised = tool.revise_deterministic(package, instruction, edit_plan=edit_plan)
        if edit_plan.mutation_required and not tool.package_changed(package, revised):
            return _needs_review(
                step,
                "Slides revision produced no visible artifact changes.",
                "这次修订没有命中可见页面。你想改哪一页或哪一段内容？",
                ["重新说明页码", "重新说明页面标题", "先查看当前 PPT"],
            )
        revised["version"] = max(coerce_positive_int(package.get("version")) + 1, 2)
        artifact = tool.persist_artifact(
            revised,
            provider=provider,
            session_id=state.message.session_id,
            task_run_id=state.task_run_id,
        )
        reply_preview = "【演示稿修订】\n" + tool.format_reply(revised, artifact=artifact)
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "slides",
                "reply_preview": reply_preview,
                "artifacts": [artifact],
                "close_title": artifact.get("title") or "slides",
            },
        )

    def _revise_canvas(self, state: WorkflowGraphState, step: PlanStep, command: WorkspaceCommand) -> WorkerResult:
        artifact = _latest_artifact(state, {"canvas"})
        scene = _artifact_preview(artifact)
        if not artifact or not isinstance(scene.get("shapes"), list) or not scene.get("shapes"):
            return _needs_review(
                step,
                "Canvas revision requires an existing canvas artifact.",
                "当前还没有可修订的画布产物。要先生成一张画布吗？",
                ["先生成画布", "重新说明要修订的产物"],
            )
        tool = self.workflow._canvas_tool()
        instruction = state.message.text
        raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
        edit_plan = tool.plan_revision(scene, instruction, raw_payload)
        revised = tool.revise_scene_deterministic(scene, instruction, edit_plan=edit_plan)
        if edit_plan.mutation_required and not _artifact_payload_changed(scene, revised):
            return _needs_review(
                step,
                "Canvas revision produced no visible artifact changes.",
                "这次修订没有命中可见节点。你想改哪个节点或哪一组内容？",
                ["重新说明节点名称", "重新说明节点编号", "先查看当前画布"],
            )
        revised["version"] = max(coerce_positive_int(scene.get("version")) + 1, 2)
        artifact = tool.generate_flow_artifact(
            title=str(revised.get("title") or artifact.get("title") or "Canvas"),
            instruction=instruction,
            llm_result={"canvas": revised},
            workspace_context=state.context.excerpt if state.context else "",
            task_run_id=state.task_run_id,
            session_id=state.message.session_id,
        )
        reply_preview = "【画布修订】\n" + tool.format_reply(artifact)
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "canvas",
                "reply_preview": reply_preview,
                "artifacts": [artifact],
                "close_title": artifact.get("title") or "canvas",
            },
        )

    def _llm_result_for_step(self, command: WorkspaceCommand, step: PlanStep) -> dict[str, Any]:
        operation = "create" if command.operation == "generate" else command.operation
        route = step.worker
        step_goal = str(step.input.get("goal") or command.target_text or command.reason or "").strip()
        requested_outputs = step.input.get("requested_outputs") if isinstance(step.input.get("requested_outputs"), list) else [route]
        result = {
            "operation": operation,
            "object": route,
            "route": route,
            "reason": command.reason,
            "artifact_goal": step_goal,
            "requested_outputs": requested_outputs,
            "all_requested_outputs": list(command.requested_outputs),
            "plan": {
                "goal": step_goal,
                "steps": [
                    {
                        "id": step.step_id,
                        "type": self._legacy_step_type(step.worker),
                        "title": step_goal or f"Generate {step.worker}",
                        "depends_on": step.depends_on,
                        "agent": step.input.get("agent"),
                    }
                ],
            },
        }
        raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
        if route in raw_payload:
            result[route] = raw_payload[route]
        for key in ("artifact_edit_plan", "task_operations", "tasks", "risks", "next_actions", "summary"):
            if key in raw_payload:
                result[key] = raw_payload[key]
        return result

    def _target_document_for_step(self, state: WorkflowGraphState, command: WorkspaceCommand) -> dict | None:
        if command.operation not in {"revise", "update"}:
            return None
        if state.context and isinstance(state.context.current_document, dict):
            return state.context.current_document
        return None

    @staticmethod
    def _legacy_step_type(worker: str) -> str:
        return {
            "doc": "sync_doc",
            "slides": "generate_slides",
            "canvas": "generate_canvas",
        }.get(worker, "reply_help")


def _message_context(state: WorkflowGraphState) -> FeishuMessageContext:
    return FeishuMessageContext(
        message_id=state.message.message_id,
        chat_id=state.message.chat_id,
        chat_type=state.message.chat_type,
        message_type="text",
        session_id=state.message.session_id,
        sender_id=state.message.sender_id or "",
        text=state.message.text,
        raw_text=state.message.raw_text or state.message.text,
        is_mentioned=state.message.is_mentioned,
    )


def _latest_artifact(state: WorkflowGraphState, artifact_types: set[str]) -> dict[str, Any] | None:
    artifacts = state.context.artifacts if state.context else []
    for item in reversed(artifacts):
        artifact_type = str(item.get("artifact_type") or "").strip()
        if artifact_type in artifact_types:
            return item
    return None


def _artifact_preview(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        return {}
    preview = artifact.get("preview")
    if isinstance(preview, dict):
        return preview
    preview_json = artifact.get("preview_json")
    if isinstance(preview_json, dict):
        return preview_json
    if not isinstance(preview_json, str) or not preview_json.strip():
        return {}
    try:
        decoded = json.loads(preview_json)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _artifact_payload_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    ignored = {"version", "revision_instruction", "artifact_edit_plan"}

    def comparable(payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in ignored}

    return comparable(before) != comparable(after)


def _needs_review(step: PlanStep, reason: str, question: str, options: list[str]) -> WorkerResult:
    return WorkerResult(
        step_id=step.step_id,
        worker=step.worker,
        ok=False,
        status="needs_review",
        error=reason,
        output={
            "clarification": {
                "question": question,
                "reason": reason,
                "options": options,
                "blocking": True,
            }
        },
    )
