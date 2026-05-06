from __future__ import annotations

from app.services.graph.state import DAGPlan, WorkerResult


def run_validator_review(plan: DAGPlan, results: dict[str, WorkerResult]) -> dict:
    missing = [step.step_id for step in plan.steps if step.step_id not in results]
    failed = [result for result in results.values() if not result.ok and result.status != "needs_review"]
    needs_review = [result for result in results.values() if result.status == "needs_review"]
    clarification = _first_clarification(needs_review)
    risks = [item.error for item in [*failed, *needs_review] if item.error]
    ok = not missing and not failed and not needs_review
    return {
        "agent": "ValidatorAgent",
        "status": "passed" if ok else "failed",
        "ok": ok,
        "summary": _summary(missing, failed, needs_review),
        "missing_outputs": missing,
        "risks": risks,
        "needs_clarification": clarification is not None,
        "clarification": clarification,
        "recommended_next_actions": _next_actions(missing, failed, needs_review),
    }


def _first_clarification(results: list[WorkerResult]) -> dict | None:
    for result in results:
        clarification = result.output.get("clarification") if isinstance(result.output, dict) else None
        if isinstance(clarification, dict):
            return clarification
    return None


def _next_actions(missing: list[str], failed: list[WorkerResult], needs_review: list[WorkerResult]) -> list[str]:
    actions: list[str] = []
    if missing:
        actions.append("retry missing graph worker steps")
    if failed:
        actions.append("inspect failed graph worker steps")
    if needs_review:
        actions.append("ask user to clarify the task target")
    return actions


def _summary(missing: list[str], failed: list[WorkerResult], needs_review: list[WorkerResult]) -> str:
    if not missing and not failed and not needs_review:
        return "All planned graph worker steps completed."
    parts: list[str] = []
    if missing:
        parts.append(f"missing={len(missing)}")
    if failed:
        parts.append(f"failed={len(failed)}")
    if needs_review:
        parts.append(f"needs_review={len(needs_review)}")
    return "Worker validation found issues: " + ", ".join(parts)
