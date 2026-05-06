from __future__ import annotations

from app.services.graph.state import DAGPlan, WorkspaceCommand


def planner_node():
    def run(state: dict) -> dict:
        raw_command = state.get("command")
        command = WorkspaceCommand.model_validate(raw_command) if raw_command else WorkspaceCommand()
        plan = DAGPlan.from_command(command)
        return {
            **state,
            "plan": plan.model_dump(mode="json"),
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.planner",
                    "status": "done",
                    "step_count": len(plan.steps),
                    "needs_confirmation": plan.needs_confirmation,
                },
            ],
        }

    return run
