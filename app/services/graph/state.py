from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


CommandMode = Literal["shortcut", "workspace_action", "chat", "clarify", "unknown"]
CommandOperation = Literal[
    "read",
    "analyze",
    "create",
    "update",
    "remove",
    "complete",
    "assign",
    "generate",
    "revise",
    "recommend",
    "chat",
    "clarify",
    "help",
    "unknown",
]
CommandObject = Literal["tasks", "task", "summary", "risks", "doc", "slides", "canvas", "delivery", "workspace", "unknown"]
WorkerKind = Literal["task", "analysis", "doc", "slides", "canvas", "delivery", "review", "reply", "help"]


class GraphMessage(BaseModel):
    message_id: str | None = None
    session_id: str
    chat_id: str | None = None
    chat_type: str = "group"
    sender_id: str | None = None
    text: str = ""
    raw_text: str = ""
    is_mentioned: bool = False


class WorkspaceCommand(BaseModel):
    route: str = ""
    mode: CommandMode = "unknown"
    operation: CommandOperation = "unknown"
    object: CommandObject = "unknown"
    target_text: str = ""
    target_owner: str | None = None
    target_status: str | None = None
    requested_outputs: list[str] = Field(default_factory=list)
    artifact_goals: dict[str, str] = Field(default_factory=dict)
    destructive: bool = False
    batch: bool = False
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: str | None = None
    reason: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "WorkspaceCommand":
        raw = payload if isinstance(payload, dict) else {}
        operation = _normalize_operation(raw.get("operation") or raw.get("action"))
        object_name = _normalize_object(raw.get("object") or raw.get("target"))
        mode = _normalize_mode(raw.get("mode"), operation=operation, object_name=object_name)
        confidence = _normalize_confidence(raw.get("confidence"))
        requested_outputs = _normalize_requested_outputs(raw.get("requested_outputs"))
        artifact_goals = _normalize_artifact_goals(raw.get("artifact_goals"))
        route = _normalize_route(raw.get("route"))
        needs_clarification = bool(raw.get("needs_clarification"))
        if confidence and confidence < 0.5:
            needs_clarification = True
        if needs_clarification:
            mode = "clarify"
            if operation == "unknown":
                operation = "clarify"

        return cls(
            route=route or _infer_route(operation, object_name, requested_outputs),
            mode=mode,
            operation=operation,
            object=object_name,
            target_text=str(raw.get("target_text") or raw.get("task_hint") or "").strip()[:160],
            target_owner=_optional_text(raw.get("target_owner") or raw.get("owner")),
            target_status=_optional_text(raw.get("target_status") or raw.get("status")),
            requested_outputs=requested_outputs,
            artifact_goals=artifact_goals,
            destructive=bool(raw.get("destructive")) or operation in {"remove"},
            batch=bool(raw.get("batch")),
            confidence=confidence,
            needs_clarification=needs_clarification,
            clarification_question=_optional_text(raw.get("clarification_question")),
            reason=str(raw.get("reason") or "").strip()[:240],
            raw_payload=dict(raw),
        )

    @classmethod
    def from_route(cls, route: str, *, reason: str = "", requested_outputs: list[str] | tuple[str, ...] = ()) -> "WorkspaceCommand":
        normalized_route = _normalize_route(route)
        if normalized_route == "status":
            return cls(route="status", mode="shortcut", operation="read", object="tasks", confidence=1.0, reason=reason)
        if normalized_route == "help":
            return cls(route="help", mode="shortcut", operation="help", object="workspace", confidence=1.0, reason=reason)
        if normalized_route == "risks":
            return cls(route="risks", mode="shortcut", operation="analyze", object="risks", confidence=1.0, reason=reason)
        if normalized_route == "summary":
            return cls(route="summary", mode="shortcut", operation="analyze", object="summary", confidence=1.0, reason=reason)
        if normalized_route == "tasks":
            return cls(route="tasks", mode="shortcut", operation="analyze", object="tasks", confidence=1.0, reason=reason)
        if normalized_route in {"doc", "slides", "canvas"}:
            return cls(
                route=normalized_route,
                mode="shortcut",
                operation="generate",
                object="workspace",
                requested_outputs=_normalize_requested_outputs(list(requested_outputs) or [normalized_route]),
                confidence=1.0,
                reason=reason,
            )
        return cls(route=normalized_route, mode="shortcut", operation="unknown", object="unknown", confidence=1.0, reason=reason)


class ContextPack(BaseModel):
    session_id: str
    excerpt: str = ""
    legacy_route: dict[str, Any] | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    current_document: dict[str, Any] | None = None
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    source_docs: list[dict[str, Any]] = Field(default_factory=list)
    user_added_materials: list[dict[str, Any]] = Field(default_factory=list)
    output_requirements: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    missing_fields: list[dict[str, Any]] = Field(default_factory=list)
    loaded_sources: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class PlanStep(BaseModel):
    step_id: str
    worker: WorkerKind
    operation: str
    depends_on: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    can_run_parallel: bool = True
    destructive: bool = False
    idempotency_key: str | None = None


class DAGPlan(BaseModel):
    plan_id: str
    steps: list[PlanStep] = Field(default_factory=list)
    needs_confirmation: bool = False
    confirmation_reason: str | None = None

    @classmethod
    def from_command(cls, command: WorkspaceCommand) -> "DAGPlan":
        steps: list[PlanStep] = []
        outputs = [item for item in command.requested_outputs if item in {"doc", "slides", "canvas"}]
        route = _normalize_route(command.route)
        if route == "delivery":
            dependencies: list[str] = []
            for output in outputs:
                step = _plan_step(worker=output, operation="generate", command=command)
                steps.append(step)
                dependencies.append(step.step_id)
            steps.append(
                _plan_step(
                    worker="delivery",
                    operation="bundle",
                    command=command,
                    can_run_parallel=False,
                    depends_on=dependencies,
                )
            )
        elif command.operation == "generate" and outputs:
            for output in outputs:
                steps.append(_plan_step(worker=output, operation="generate", command=command))
        elif route == "status":
            steps.append(_plan_step(worker="task", operation="read", command=command))
        elif route in {"summary", "risks"} or command.operation == "analyze":
            steps.append(_plan_step(worker="analysis", operation="analyze", command=command))
        elif route == "help" or command.operation == "help":
            steps.append(_plan_step(worker="help", operation="help", command=command, can_run_parallel=False))
        elif command.object in {"task", "tasks"} or command.operation in {"read", "remove", "complete", "assign"}:
            steps.append(_plan_step(worker="task", operation=command.operation, command=command))
        elif command.object in {"doc", "slides", "canvas"}:
            steps.append(_plan_step(worker=command.object, operation=command.operation, command=command))
        elif command.object == "delivery":
            steps.append(_plan_step(worker="delivery", operation=command.operation, command=command))
        else:
            steps.append(_plan_step(worker="reply", operation=command.operation, command=command, can_run_parallel=False))

        return cls(
            plan_id=f"plan_{uuid.uuid4().hex[:10]}",
            steps=steps,
            needs_confirmation=command.needs_clarification or command.destructive,
            confirmation_reason=command.clarification_question if command.needs_clarification else None,
        )


class WorkerResult(BaseModel):
    step_id: str
    worker: str
    ok: bool
    status: Literal["skipped", "running", "done", "failed", "needs_review"] = "done"
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float | None = None


class ReviewReport(BaseModel):
    ok: bool = True
    missing_outputs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification: dict[str, Any] | None = None
    recommended_next_actions: list[str] = Field(default_factory=list)


class ReplyPackage(BaseModel):
    text: str = ""
    card: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] | None = None
    reply_sent: bool = False
    reply_error: str | None = None


class WorkflowGraphState(BaseModel):
    message: GraphMessage
    task_run_id: str | None = None
    active_episode_id: int | None = None
    confirmation_answer: dict[str, Any] | None = None
    command: WorkspaceCommand | None = None
    context: ContextPack | None = None
    plan: DAGPlan | None = None
    worker_results: dict[str, WorkerResult] = Field(default_factory=dict)
    review: ReviewReport | None = None
    reply: ReplyPackage | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)


def _plan_step(
    *,
    worker: str,
    operation: str,
    command: WorkspaceCommand,
    can_run_parallel: bool = True,
    depends_on: list[str] | None = None,
) -> PlanStep:
    step_id = f"{worker}_{operation or 'unknown'}"
    payload = {
        "operation": operation,
        "object": command.object,
        "target_text": command.target_text,
        "target_owner": command.target_owner,
        "goal": _step_goal(command, worker),
        "agent": _agent_label(worker),
        "requested_outputs": [worker] if worker in {"doc", "slides", "canvas"} else command.requested_outputs,
        "all_requested_outputs": command.requested_outputs,
    }
    idempotency_source = f"{worker}:{operation}:{command.object}:{command.target_text}:{command.target_owner}"
    return PlanStep(
        step_id=step_id,
        worker=worker,  # type: ignore[arg-type]
        operation=operation or "unknown",
        depends_on=list(depends_on or []),
        input={key: value for key, value in payload.items() if value not in (None, "", [])},
        can_run_parallel=can_run_parallel,
        destructive=command.destructive,
        idempotency_key=hashlib.sha256(idempotency_source.encode("utf-8")).hexdigest()[:24],
    )


def _normalize_mode(value: Any, *, operation: str, object_name: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "action": "workspace_action",
        "workspace": "workspace_action",
        "question": "chat",
        "ask": "chat",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {"shortcut", "workspace_action", "chat", "clarify", "unknown"}:
        return normalized
    if operation in {"chat"}:
        return "chat"
    if operation in {"clarify"}:
        return "clarify"
    if operation != "unknown" or object_name != "unknown":
        return "workspace_action"
    return "unknown"


def _normalize_operation(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "analyse": "analyze",
        "analysis": "analyze",
        "summarize": "analyze",
        "summary": "analyze",
        "delete": "remove",
        "cancel": "remove",
        "finish": "complete",
        "finished": "complete",
        "done": "complete",
        "completed": "complete",
        "claim": "assign",
        "owner": "assign",
        "make": "generate",
        "write": "generate",
        "sync": "generate",
        "create_artifact": "generate",
        "edit": "revise",
        "modify": "revise",
        "next_action": "recommend",
        "status": "read",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {
        "read",
        "analyze",
        "create",
        "update",
        "remove",
        "complete",
        "assign",
        "generate",
        "revise",
        "recommend",
        "chat",
        "clarify",
        "help",
        "unknown",
    }
    return normalized if normalized in allowed else "unknown"


def _normalize_object(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "todo": "tasks",
        "todos": "tasks",
        "risk": "risks",
        "blocker": "risks",
        "blockers": "risks",
        "summaries": "summary",
        "document": "doc",
        "feishu_doc": "doc",
        "ppt": "slides",
        "presentation": "slides",
        "deck": "slides",
        "whiteboard": "canvas",
        "board": "canvas",
        "diagram": "canvas",
        "flowchart": "canvas",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {"tasks", "task", "summary", "risks", "doc", "slides", "canvas", "delivery", "workspace", "unknown"}
    return normalized if normalized in allowed else "unknown"


def _normalize_route(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "todo": "tasks",
        "todos": "tasks",
        "risk": "risks",
        "blocker": "risks",
        "blockers": "risks",
        "document": "doc",
        "feishu_doc": "doc",
        "ppt": "slides",
        "presentation": "slides",
        "whiteboard": "canvas",
        "diagram": "canvas",
        "flowchart": "canvas",
        "recommend": "status",
        "next_action": "status",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {"status", "summary", "tasks", "risks", "doc", "slides", "canvas", "delivery", "help", "unknown"}
    return normalized if normalized in allowed else ""


def _infer_route(operation: str, object_name: str, requested_outputs: list[str]) -> str:
    if operation in {"help", "clarify"}:
        return "help"
    if operation == "recommend" and object_name == "workspace":
        return "status"
    if operation == "read" and object_name in {"task", "tasks"}:
        return "status"
    if operation == "analyze":
        if object_name in {"summary", "risks", "tasks"}:
            return object_name
        return "summary"
    if operation in {"create", "update", "remove", "complete", "assign"} and object_name in {"task", "tasks"}:
        return "tasks"
    if operation == "generate" and requested_outputs:
        return requested_outputs[0]
    if object_name in {"doc", "slides", "canvas", "delivery"}:
        return object_name
    return "unknown"


def _normalize_requested_outputs(value: Any) -> list[str]:
    raw_items = [value] if isinstance(value, str) else value if isinstance(value, list | tuple) else []
    outputs: list[str] = []
    for item in raw_items:
        output = _normalize_object(item)
        if output in {"doc", "slides", "canvas"} and output not in outputs:
            outputs.append(output)
    return outputs


def _normalize_artifact_goals(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    goals: dict[str, str] = {}
    for key, raw_goal in value.items():
        output = _normalize_object(key)
        if output not in {"doc", "slides", "canvas"}:
            continue
        goal = str(raw_goal or "").strip()
        if goal:
            goals[output] = goal[:240]
    return goals


def _step_goal(command: WorkspaceCommand, worker: str) -> str:
    if worker in command.artifact_goals:
        return command.artifact_goals[worker]
    base_goal = command.target_text or command.reason
    if base_goal:
        return base_goal
    if worker == "doc":
        return "Draft a concise collaboration document from the workspace context."
    if worker == "slides":
        return "Create a presentation outline from the workspace context."
    if worker == "canvas":
        return "Create a visual canvas or diagram from the workspace context."
    return command.reason or command.target_text


def _agent_label(worker: str) -> str:
    return {
        "task": "TaskAgent",
        "analysis": "AnalysisAgent",
        "doc": "DocAgent",
        "slides": "SlidesAgent",
        "canvas": "CanvasAgent",
        "delivery": "DeliveryAgent",
        "review": "ReviewerAgent",
        "reply": "ReplyAgent",
        "help": "HelpAgent",
    }.get(worker, "WorkerAgent")


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(confidence, 1.0))


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:120] if text else None
