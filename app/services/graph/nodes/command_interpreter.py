from __future__ import annotations

import logging
from typing import Any

from app.services.graph.state import WorkspaceCommand

logger = logging.getLogger(__name__)


def command_interpreter_node(workflow: Any):
    def run(state: dict) -> dict:
        message = state.get("message") or {}
        context = state.get("context") or {}
        instruction = str(message.get("text") or "").strip()
        llm_service = getattr(workflow, "llm_service", None)
        if llm_service is None or not getattr(llm_service, "is_configured", lambda: False)():
            legacy_command = _command_from_legacy_route(context)
            if legacy_command is not None:
                return _command_patch(state, legacy_command, status="legacy_route")
            command = WorkspaceCommand(
                mode="clarify",
                operation="clarify",
                object="unknown",
                confidence=0.0,
                needs_clarification=True,
                clarification_question="LLM is not configured, keep using legacy workflow.",
                reason="langgraph shadow command interpreter skipped because LLM is unavailable",
            )
            return _command_patch(state, command, status="skipped")

        try:
            raw_command = llm_service.interpret_workspace_command(
                str(context.get("excerpt") or ""),
                instruction,
            )
            command = WorkspaceCommand.from_payload(raw_command)
            command = _apply_legacy_hints(command, context)
            return _command_patch(state, command, status="done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangGraph command interpreter failed: %s", exc)
            command = WorkspaceCommand(
                mode="clarify",
                operation="clarify",
                object="unknown",
                confidence=0.0,
                needs_clarification=True,
                clarification_question="Command interpretation failed; keep using legacy workflow.",
                reason=str(exc)[:240],
            )
            return {
                **state,
                **_command_patch(state, command, status="failed"),
                "errors": [*list(state.get("errors") or []), {"node": "graph.command_interpreter", "error": str(exc)}],
            }

    return run


def _apply_legacy_hints(command: WorkspaceCommand, context: dict) -> WorkspaceCommand:
    legacy_route = context.get("legacy_route") if isinstance(context.get("legacy_route"), dict) else {}
    updates: dict[str, Any] = {}
    if not (command.route and command.route != "unknown"):
        route = str(legacy_route.get("route") or "").strip().lower()
        if route in {"status", "tasks", "summary", "risks", "doc", "slides", "canvas", "delivery", "help"}:
            updates["route"] = route
    legacy_outputs = _legacy_requested_outputs(legacy_route)
    merged_outputs = _merge_requested_outputs(command.requested_outputs, legacy_outputs)
    if merged_outputs != command.requested_outputs:
        updates["requested_outputs"] = merged_outputs
    if merged_outputs and _should_promote_artifact_generation(command):
        updates.update(
            {
                "operation": "generate",
                "object": "workspace",
                "route": merged_outputs[0],
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    if legacy_route.get("needs_clarification"):
        clarification = legacy_route.get("clarification") if isinstance(legacy_route.get("clarification"), dict) else {}
        raw_payload = dict(command.raw_payload)
        if clarification:
            raw_payload["clarification"] = clarification
        updates.update(
            {
                "needs_clarification": True,
                "clarification_question": str(
                    legacy_route.get("clarification_question")
                    or clarification.get("question")
                    or command.clarification_question
                    or ""
                ).strip()
                or None,
                "reason": command.reason or str(legacy_route.get("reason") or clarification.get("reason") or "").strip(),
                "raw_payload": raw_payload,
            }
        )
    return command.model_copy(update=updates) if updates else command


def _legacy_requested_outputs(legacy_route: dict[str, Any]) -> list[str]:
    raw_outputs = legacy_route.get("requested_outputs")
    values = raw_outputs if isinstance(raw_outputs, list | tuple) else [raw_outputs] if isinstance(raw_outputs, str) else []
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
    outputs: list[str] = []
    for item in values:
        output = aliases.get(str(item or "").strip().lower(), str(item or "").strip().lower())
        if output in {"doc", "slides", "canvas"} and output not in outputs:
            outputs.append(output)
    return outputs


def _merge_requested_outputs(primary: list[str], legacy: list[str]) -> list[str]:
    merged: list[str] = []
    seed = [*legacy, *primary] if legacy else list(primary)
    for item in seed:
        output = str(item or "").strip().lower()
        if output in {"doc", "slides", "canvas"} and output not in merged:
            merged.append(output)
    return merged


def _should_promote_artifact_generation(command: WorkspaceCommand) -> bool:
    if command.operation in {"revise", "update", "remove"}:
        return False
    return (
        command.operation in {"analyze", "chat", "unknown", "clarify"}
        or command.route in {"summary", "unknown"}
        or command.object in {"summary", "unknown"}
    )


def _adopt_legacy_route(command: WorkspaceCommand, context: dict) -> WorkspaceCommand:
    if command.route and command.route != "unknown":
        return command
    legacy_route = context.get("legacy_route") if isinstance(context.get("legacy_route"), dict) else {}
    route = str(legacy_route.get("route") or "").strip().lower()
    if route not in {"status", "tasks", "summary", "risks", "doc", "slides", "canvas", "delivery", "help"}:
        return command
    return command.model_copy(update={"route": route})


def _command_from_legacy_route(context: dict) -> WorkspaceCommand | None:
    legacy_route = context.get("legacy_route") if isinstance(context.get("legacy_route"), dict) else {}
    route = str(legacy_route.get("route") or "").strip()
    if not route:
        return None
    command = WorkspaceCommand.from_route(
        route,
        reason=str(legacy_route.get("reason") or ""),
        requested_outputs=legacy_route.get("requested_outputs") if isinstance(legacy_route.get("requested_outputs"), list) else [],
    )
    return command if command.route and command.route != "unknown" else None


def _command_patch(state: dict, command: WorkspaceCommand, *, status: str) -> dict:
    return {
        **state,
        "command": command.model_dump(mode="json"),
        "trace": [
            *list(state.get("trace") or []),
            {
                "node": "graph.command_interpreter",
                "status": status,
                "route": command.route,
                "operation": command.operation,
                "object": command.object,
                "confidence": command.confidence,
            },
        ],
    }
