from __future__ import annotations

from typing import Any

from app.services.graph.state import WorkspaceCommand


def shortcut_node(workflow: Any):
    def run(state: dict) -> dict:
        message = state.get("message") or {}
        text = str(message.get("text") or "").strip()
        router = getattr(workflow, "request_router", None)
        if router is None:
            return dict(state)
        decision = router.route_by_exact_rule(text)
        if decision is None:
            return dict(state)
        command = WorkspaceCommand.from_route(
            decision.route,
            reason=decision.reason,
            requested_outputs=list(decision.requested_outputs),
        )
        return {
            **state,
            "command": command.model_dump(mode="json"),
            "trace": _append_trace(
                state,
                {
                    "node": "graph.shortcut",
                    "status": "done",
                    "route": decision.route,
                    "source": decision.source,
                },
            ),
        }

    return run


def route_after_shortcut(state: dict) -> str:
    return "planned" if state.get("command") else "interpret"


def _append_trace(state: dict, item: dict) -> list[dict]:
    return [*list(state.get("trace") or []), item]
