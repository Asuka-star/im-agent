from __future__ import annotations

from typing import Any

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.graph.state import ContextPack, PlanStep, WorkerResult, WorkflowGraphState, WorkspaceCommand
from app.services.text_analysis import build_next_actions, infer_risks, normalize_tasks
from app.services.tools.task_operation_tool import TaskOperationTool


class TaskWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        command = state.command or WorkspaceCommand()
        context = state.context or ContextPack(session_id=state.message.session_id)
        tasks = _task_items(context.tasks)
        if command.operation == "read":
            return self._read_tasks(state, step)
        if command.operation == "complete":
            return self._complete_tasks(state, step, command, tasks)
        if command.operation == "assign":
            return self._assign_task(state, step, command, tasks)
        if command.operation == "update":
            return self._update_task(state, step, command, tasks)
        if command.operation == "remove":
            return self._remove_task(state, step, command, tasks)
        if command.operation == "create":
            return self._create_task(state, step, command, tasks)
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=False,
            status="failed",
            error=f"unsupported task operation: {command.operation}",
        )

    def _read_tasks(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        message = _message_context(state)
        result = self.workflow.status_execution.prepare_status_execution(
            message,
            llm_result={"operation": "read", "object": "tasks", "route": "status"},
            active_episode_id=state.active_episode_id,
            task_run_id=state.task_run_id,
        )
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "status",
                "reply_preview": result.get("reply_preview"),
                "artifacts": result.get("artifacts", []),
            },
        )

    def _complete_tasks(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        command: WorkspaceCommand,
        tasks: list[TaskItem],
    ) -> WorkerResult:
        updated_tasks = [task.model_copy(deep=True) for task in tasks]
        updated: list[TaskItem] = []
        if command.batch:
            targets = _match_tasks(updated_tasks, command)
            for index, task in enumerate(updated_tasks):
                if task not in targets:
                    continue
                updated_task = task.model_copy(update={"status": command.target_status or "done"})
                updated_tasks[index] = updated_task
                updated.append(updated_task)
            if not updated:
                return _needs_review(step, "No task matched the batch complete command.")
        else:
            intent_result = {
                "intent": "task_status_update",
                "status": command.target_status or "done",
                "task_hint": command.target_text,
                "actor": _actor_payload(command),
            }
            resolved = TaskOperationTool.resolve_status_update_from_intent(
                updated_tasks,
                intent_result,
                actor_names=self.workflow._sender_actor_names_for_message(_message_context(state)),
                source_text=state.message.text,
            )
            clarification = resolved.get("clarification") if isinstance(resolved, dict) else None
            if isinstance(clarification, dict):
                return _needs_review(step, clarification.get("reason") or "Task completion needs clarification.", clarification)
            if not resolved.get("updated"):
                return _needs_review(step, "No task matched the complete command.")
            updated_tasks = normalize_task_dates(normalize_tasks(resolved["tasks"]))
            updated_task = resolved.get("updated_task")
            if isinstance(updated_task, TaskItem):
                updated = [updated_task]

        return self._persist_task_snapshot(
            state,
            step,
            updated_tasks,
            summary=f"Updated {len(updated)} task(s) to {command.target_status or 'done'}.",
            updated_tasks=updated,
        )

    def _assign_task(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        command: WorkspaceCommand,
        tasks: list[TaskItem],
    ) -> WorkerResult:
        if not command.target_owner:
            return _needs_review(step, "Task assignment command is missing target_owner.")
        intent_result = {
            "intent": "task_assignment",
            "task_hint": command.target_text,
            "target_task": {"owner": ""},
            "assignee": {"source": "literal", "text": command.target_owner},
            "reason": command.reason,
        }
        resolved = TaskOperationTool.resolve_assignment_from_intent(
            tasks,
            intent_result,
            actor_names=self.workflow._sender_actor_names_for_message(_message_context(state)),
            source_text=state.message.text,
        )
        clarification = resolved.get("clarification") if isinstance(resolved, dict) else None
        if isinstance(clarification, dict):
            return _needs_review(step, clarification.get("reason") or "Task assignment needs clarification.", clarification)
        if not resolved.get("updated"):
            return _needs_review(step, "No task matched the assignment command.")
        return self._persist_task_snapshot(
            state,
            step,
            normalize_task_dates(normalize_tasks(resolved["tasks"])),
            summary="Updated task owner.",
            updated_tasks=[resolved["updated_task"]] if isinstance(resolved.get("updated_task"), TaskItem) else [],
        )

    def _update_task(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        command: WorkspaceCommand,
        tasks: list[TaskItem],
    ) -> WorkerResult:
        operations = _task_operations(command)
        if operations:
            updated_tasks = normalize_task_dates(
                normalize_tasks(TaskOperationTool.apply_llm_operations(tasks, operations))
            )
            changed_tasks = _changed_tasks(tasks, updated_tasks)
            if not changed_tasks and len(updated_tasks) == len(tasks):
                return _needs_review(step, "Task operations did not match any existing task.")
            return self._persist_task_snapshot(
                state,
                step,
                updated_tasks,
                summary=f"Applied {len(operations)} task operation(s).",
                updated_tasks=changed_tasks,
            )
        if command.target_status:
            return self._complete_tasks(state, step, command, tasks)
        if command.target_owner:
            return self._assign_task(state, step, command, tasks)
        return _needs_review(
            step,
            "Task update command is missing target_status or target_owner.",
            {
                "question": "你想更新任务的状态、负责人，还是任务内容？",
                "reason": "任务更新需要明确要修改的字段，避免误改当前任务快照。",
                "options": ["更新状态", "更新负责人", "重新说明任务内容"],
                "blocking": True,
            },
        )

    def _remove_task(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        command: WorkspaceCommand,
        tasks: list[TaskItem],
    ) -> WorkerResult:
        matches = _match_tasks(tasks, command)
        if len(matches) != 1:
            clarification = {
                "question": "你要删除的是哪一项任务？",
                "reason": "删除任务需要唯一匹配，避免误删。",
                "options": [TaskOperationTool.task_option_label(task) for task in matches[:5]]
                or ["先查看任务列表", "重新说明任务标题和负责人"],
                "candidates": [task.model_dump(mode="json") for task in matches[:5]],
                "blocking": True,
            }
            return _needs_review(step, clarification["reason"], clarification)
        target = matches[0]
        updated_tasks = [task for task in tasks if task != target]
        return self._persist_task_snapshot(
            state,
            step,
            normalize_task_dates(normalize_tasks(updated_tasks)),
            summary=f"Removed task: {target.owner} - {target.title}.",
            updated_tasks=[],
        )

    def _create_task(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        command: WorkspaceCommand,
        tasks: list[TaskItem],
    ) -> WorkerResult:
        title = command.target_text.strip()
        if not title:
            return _needs_review(step, "Create task command is missing target_text.")
        created = TaskItem(
            title=title,
            owner=command.target_owner or "TBD",
            priority="medium",
            due_date="TBD",
            status=command.target_status or "draft",
            notes=command.reason,
        )
        return self._persist_task_snapshot(
            state,
            step,
            normalize_task_dates(normalize_tasks([*tasks, created])),
            summary=f"Created task: {created.owner} - {created.title}.",
            updated_tasks=[created],
        )

    def _persist_task_snapshot(
        self,
        state: WorkflowGraphState,
        step: PlanStep,
        tasks: list[TaskItem],
        *,
        summary: str,
        updated_tasks: list[TaskItem],
    ) -> WorkerResult:
        tasks = normalize_task_dates(normalize_tasks(tasks))
        risks = infer_risks(tasks)
        analysis = AnalyzeResponse(
            session_id=state.message.session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=build_next_actions(tasks, risks),
            agent_traces=[
                AgentTrace(agent="graph.task_worker", summary=summary),
            ],
        )
        self.workflow.memory_service.save_round(
            session_id=state.message.session_id,
            analysis=analysis,
            episode_id=state.active_episode_id,
            source_message_id=state.message.message_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        if len(updated_tasks) == 1 and step.operation == "complete":
            reply_preview = self.workflow.response_formatter.format_task_status_update_reply(
                updated_tasks[0],
                updated_tasks[0].status,
            )
        else:
            reply_preview = self.workflow.response_formatter.format_analysis_reply(analysis, "tasks")
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "tasks",
                "reply_preview": reply_preview,
                "analysis": analysis.model_dump(mode="json"),
                "updated_task_count": len(updated_tasks),
                "task_count": len(tasks),
                "artifacts": [],
            },
        )


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


def _task_items(raw_tasks: list[dict[str, Any]]) -> list[TaskItem]:
    tasks: list[TaskItem] = []
    for item in raw_tasks:
        try:
            tasks.append(TaskItem.model_validate(item))
        except Exception:
            continue
    return tasks


def _match_tasks(tasks: list[TaskItem], command: WorkspaceCommand) -> list[TaskItem]:
    candidates = [
        task
        for task in tasks
        if str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
    ]
    if command.target_owner:
        candidates = [
            task
            for task in candidates
            if _contains_label(command.target_owner, task.owner) or _contains_label(task.owner, command.target_owner)
        ]
    if command.target_text:
        candidates = [
            task
            for task in candidates
            if TaskOperationTool.line_matches_task_title(command.target_text, task.title)
            or TaskOperationTool.line_matches_task_title(f"{command.target_text} {task.owner}", task.title)
        ]
    return candidates


def _actor_payload(command: WorkspaceCommand) -> dict[str, str]:
    if command.target_owner:
        return {"source": "literal", "text": command.target_owner}
    return {"source": "unknown", "text": ""}


def _task_operations(command: WorkspaceCommand) -> list[dict[str, Any]]:
    raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
    operations = raw_payload.get("task_operations")
    if not isinstance(operations, list):
        operations = raw_payload.get("operations")
    return [item for item in operations if isinstance(item, dict)] if isinstance(operations, list) else []


def _changed_tasks(before: list[TaskItem], after: list[TaskItem]) -> list[TaskItem]:
    before_payloads = [task.model_dump(mode="json") for task in before]
    changed: list[TaskItem] = []
    for index, task in enumerate(after):
        payload = task.model_dump(mode="json")
        if index >= len(before_payloads) or before_payloads[index] != payload:
            changed.append(task)
    return changed


def _contains_label(left: str | None, right: str | None) -> bool:
    left_key = "".join(str(left or "").split()).lower()
    right_key = "".join(str(right or "").split()).lower()
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def _needs_review(step: PlanStep, reason: str, clarification: dict[str, Any] | None = None) -> WorkerResult:
    output: dict[str, Any] = {}
    if clarification:
        output["clarification"] = clarification
    return WorkerResult(
        step_id=step.step_id,
        worker=step.worker,
        ok=False,
        status="needs_review",
        output=output,
        error=reason,
    )
