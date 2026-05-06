from __future__ import annotations

from app.services.graph.reviewers import run_shield_review, run_validator_review
from app.services.graph.state import DAGPlan, ReviewReport, WorkerResult, WorkspaceCommand


def reviewer_node():
    def run(state: dict) -> dict:
        plan = DAGPlan.model_validate(state.get("plan") or {"plan_id": "empty", "steps": []})
        raw_results = state.get("worker_results") if isinstance(state.get("worker_results"), dict) else {}
        results = {
            step_id: WorkerResult.model_validate(payload)
            for step_id, payload in raw_results.items()
            if isinstance(payload, dict)
        }
        command = WorkspaceCommand.model_validate(state.get("command") or {})
        checks = [
            run_validator_review(plan, results),
            run_shield_review(
                command,
                plan,
                confirmation_answer=state.get("confirmation_answer") if isinstance(state.get("confirmation_answer"), dict) else None,
            ),
        ]
        missing = _collect_list(checks, "missing_outputs")
        risks = _collect_list(checks, "risks")
        clarification = _first_clarification(checks)
        next_actions = _collect_list(checks, "recommended_next_actions")
        review = ReviewReport(
            ok=all(bool(check.get("ok")) for check in checks),
            missing_outputs=missing,
            risks=risks,
            checks=checks,
            needs_clarification=clarification is not None,
            clarification=clarification,
            recommended_next_actions=next_actions,
        )
        return {
            **state,
            "review": review.model_dump(mode="json"),
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.reviewer",
                    "status": "done",
                    "ok": review.ok,
                    "missing_count": len(missing),
                    "risk_count": len(review.risks),
                    "checks": [
                        {
                            "agent": check.get("agent"),
                            "status": check.get("status"),
                            "ok": check.get("ok"),
                        }
                        for check in checks
                    ],
                    "needs_clarification": review.needs_clarification,
                },
            ],
        }

    return run


def route_after_review(state: dict) -> str:
    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    return "clarify" if review.get("needs_clarification") else "reply"


def _first_clarification(checks: list[dict]) -> dict | None:
    for check in checks:
        clarification = check.get("clarification") if isinstance(check.get("clarification"), dict) else None
        if isinstance(clarification, dict):
            return clarification
    return None


def _collect_list(checks: list[dict], field: str) -> list[str]:
    values: list[str] = []
    for check in checks:
        raw_items = check.get(field)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            value = str(item or "").strip()
            if value and value not in values:
                values.append(value)
    return values
