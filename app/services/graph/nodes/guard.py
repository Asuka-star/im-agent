from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.task import TaskItem
from app.services.graph.state import ContextPack, ReviewReport, WorkspaceCommand
from app.services.tools.task_operation_tool import TaskOperationTool


def guard_node(workflow: Any):
    def run(state: dict) -> dict:
        command = WorkspaceCommand.model_validate(state.get("command") or {})
        context = ContextPack.model_validate(state.get("context") or {"session_id": ""})
        clarification = _clarification_for_command(
            command,
            context,
            confirmation_answer=state.get("confirmation_answer") if isinstance(state.get("confirmation_answer"), dict) else None,
        )
        if clarification:
            review = ReviewReport(
                ok=False,
                needs_clarification=True,
                clarification=clarification,
                risks=[clarification["reason"]],
            )
            return {
                **state,
                "review": review.model_dump(mode="json"),
                "trace": _trace(state, "clarify", clarification["reason"]),
            }
        return {
            **state,
            "trace": _trace(state, "done", "command accepted"),
        }

    return run


def route_after_guard(state: dict) -> str:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    return "clarify" if review.get("needs_clarification") else "execute"


def _clarification_for_command(
    command: WorkspaceCommand,
    context: ContextPack,
    *,
    confirmation_answer: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    confirmed_resume = bool(confirmation_answer and confirmation_answer.get("confirmed"))
    if command.needs_clarification:
        seeded_clarification = _seeded_clarification(command)
        if seeded_clarification:
            return seeded_clarification
        return _clarification(
            command.clarification_question or "请再明确一下你想让我处理什么？",
            command.reason or "Command interpreter marked this request as ambiguous.",
        )
    if command.confidence and command.confidence < 0.75:
        return _clarification(
            "我不太确定这条指令的目标，可以再具体说明一下吗？",
            f"意图识别置信度较低：{command.confidence:.2f}",
        )
    missing_outputs = _missing_field(context, "requested_outputs")
    if missing_outputs or (command.operation == "generate" and not command.requested_outputs and command.route != "delivery" and command.object != "delivery"):
        return _clarification(
            "你想生成文档、PPT、画布，还是多个都要？",
            _missing_reason(missing_outputs, "生成类请求缺少明确的产物类型。"),
            options=["文档", "PPT", "画布", "文档 + PPT + 画布"],
        )
    missing_document = _missing_field(context, "current_document")
    if missing_document or (command.object == "doc" and command.operation in {"revise", "update"} and context.current_document is None):
        return _clarification(
            "你想更新哪一份协作文档？",
            _missing_reason(missing_document, "文档修订需要先锁定目标文档，避免把修改写到错误的文档。"),
            options=["当前文档", "上一份文档", "重新说明文档标题"],
        )
    if command.operation == "complete" and command.batch and not command.target_owner and not command.target_text:
        return _clarification(
            "你想批量完成谁名下、或哪一类任务？",
            "批量完成任务需要明确范围，避免误改所有任务。",
        )
    if command.object in {"task", "tasks"} and command.operation in {"remove", "complete", "assign"}:
        tasks = _task_items(context.tasks)
        missing_tasks = _missing_field(context, "tasks")
        if missing_tasks or not tasks:
            return _clarification(
                "当前没有可安全匹配的任务。要不要先查看任务列表？",
                _missing_reason(missing_tasks, "任务写操作需要先找到当前任务快照。"),
                options=["先查看任务列表", "重新说明任务标题和负责人"],
            )
        if command.operation == "remove":
            matches = _match_tasks(tasks, command)
            if len(matches) != 1:
                return _clarification(
                    f"你要删除的是哪一项任务？",
                    "删除任务需要唯一匹配，避免误删。",
                    options=[TaskOperationTool.task_option_label(task) for task in matches[:5]]
                    or ["先查看任务列表", "重新说明任务标题和负责人"],
                    candidates=[task.model_dump(mode="json") for task in matches[:5]],
                )
    if command.destructive and settings.langgraph_require_confirm_destructive and not confirmed_resume:
        return _clarification(
            "这个操作会修改或删除现有任务，请确认是否继续。",
            "配置要求 destructive 操作先经过用户确认。",
            options=["确认继续", "取消操作"],
        )
    return None


def _missing_field(context: ContextPack, field: str) -> dict[str, Any] | None:
    for item in context.missing_fields:
        if not isinstance(item, dict):
            continue
        if str(item.get("field") or "") == field:
            return item
    return None


def _missing_reason(item: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(item, dict):
        return fallback
    reason = str(item.get("reason") or "").strip()
    return reason or fallback


def _seeded_clarification(command: WorkspaceCommand) -> dict[str, Any] | None:
    raw_payload = command.raw_payload if isinstance(command.raw_payload, dict) else {}
    clarification = raw_payload.get("clarification") if isinstance(raw_payload.get("clarification"), dict) else None
    if not clarification:
        return None
    question = str(clarification.get("question") or command.clarification_question or "").strip()
    reason = str(clarification.get("reason") or command.reason or "").strip()
    if not question or not reason:
        return None
    options = clarification.get("options") if isinstance(clarification.get("options"), list) else None
    candidates = clarification.get("candidates") if isinstance(clarification.get("candidates"), list) else None
    return _clarification(question, reason, options=options, candidates=candidates)


def _clarification(
    question: str,
    reason: str,
    *,
    options: list[str] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "question": question,
        "reason": reason,
        "options": options or ["重新说明", "先查看任务列表"],
        "candidates": candidates or [],
        "blocking": True,
    }


def _match_tasks(tasks: list[TaskItem], command: WorkspaceCommand) -> list[TaskItem]:
    active = [
        task
        for task in tasks
        if str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
    ]
    if command.target_owner:
        active = [
            task
            for task in active
            if _contains_label(command.target_owner, task.owner) or _contains_label(task.owner, command.target_owner)
        ]
    target_text = command.target_text.strip()
    if target_text:
        active = [
            task
            for task in active
            if TaskOperationTool.line_matches_task_title(target_text, task.title)
            or TaskOperationTool.line_matches_task_title(f"{target_text} {task.owner}", task.title)
        ]
    return active


def _task_items(raw_tasks: list[dict[str, Any]]) -> list[TaskItem]:
    result: list[TaskItem] = []
    for item in raw_tasks:
        try:
            result.append(TaskItem.model_validate(item))
        except Exception:
            continue
    return result


def _contains_label(left: str | None, right: str | None) -> bool:
    left_key = "".join(str(left or "").split()).lower()
    right_key = "".join(str(right or "").split()).lower()
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def _trace(state: dict, status: str, reason: str) -> list[dict]:
    return [
        *list(state.get("trace") or []),
        {
            "node": "graph.guard",
            "status": status,
            "reason": reason,
        },
    ]
