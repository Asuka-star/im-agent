from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services.graph.state import DAGPlan, WorkspaceCommand


def run_shield_review(
    command: WorkspaceCommand,
    plan: DAGPlan,
    *,
    confirmation_answer: dict[str, Any] | None = None,
) -> dict:
    confirmed = bool(confirmation_answer and confirmation_answer.get("confirmed"))
    destructive_steps = [step.step_id for step in plan.steps if step.destructive]
    risks: list[str] = []
    recommended_next_actions: list[str] = []
    clarification = None
    ok = True

    if destructive_steps and settings.langgraph_require_confirm_destructive and not confirmed:
        ok = False
        risks.append("destructive operation reached review without confirmed user approval")
        recommended_next_actions.append("ask user to confirm destructive operation")
        clarification = {
            "question": "这个操作会修改或删除现有内容，请确认是否继续。",
            "reason": "ShieldAgent blocked an unconfirmed destructive graph operation.",
            "options": ["确认继续", "取消操作"],
            "blocking": True,
        }

    if command.batch and command.operation in {"remove", "complete", "assign"}:
        risks.append("batch task mutation should remain scoped and auditable")
        recommended_next_actions.append("keep batch task mutation scope visible in the reply")

    return {
        "agent": "ShieldAgent",
        "status": "passed" if ok else "blocked",
        "ok": ok,
        "summary": _summary(ok, destructive_steps, command.batch),
        "missing_outputs": [],
        "risks": risks,
        "needs_clarification": clarification is not None,
        "clarification": clarification,
        "recommended_next_actions": recommended_next_actions,
    }


def _summary(ok: bool, destructive_steps: list[str], batch: bool) -> str:
    if ok and not destructive_steps and not batch:
        return "No risky graph operation detected."
    if ok:
        labels: list[str] = []
        if destructive_steps:
            labels.append(f"destructive_steps={len(destructive_steps)}")
        if batch:
            labels.append("batch=true")
        return "Safety review passed with caution: " + ", ".join(labels)
    return "Safety review blocked execution result pending user confirmation."
