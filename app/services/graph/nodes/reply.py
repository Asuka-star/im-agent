from __future__ import annotations

from app.services.graph.state import ReplyPackage, WorkerResult


def reply_node():
    def run(state: dict) -> dict:
        raw_results = state.get("worker_results") if isinstance(state.get("worker_results"), dict) else {}
        results = [
            WorkerResult.model_validate(payload)
            for payload in raw_results.values()
            if isinstance(payload, dict)
        ]
        reply = _compose_reply(results)
        return {
            **state,
            "reply": reply.model_dump(mode="json"),
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.reply",
                    "status": "done",
                    "artifact_count": len(reply.artifacts),
                    "has_text": bool(reply.text),
                },
            ],
        }

    return run


def clarification_node():
    def run(state: dict) -> dict:
        review = state.get("review") if isinstance(state.get("review"), dict) else {}
        clarification = review.get("clarification") if isinstance(review.get("clarification"), dict) else {}
        question = str(clarification.get("question") or "请再明确一下你的目标。").strip()
        reply = ReplyPackage(text=question, card={"clarification": clarification})
        return {
            **state,
            "reply": reply.model_dump(mode="json"),
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.clarification",
                    "status": "pending",
                },
            ],
        }

    return run


def _compose_reply(results: list[WorkerResult]) -> ReplyPackage:
    reply_parts: list[str] = []
    artifacts: list[dict] = []
    analysis: dict | None = None
    failed_parts: list[str] = []
    for result in results:
        if not result.ok:
            failed_parts.append(_failure_summary(result))
            continue
        preview = str(result.output.get("reply_preview") or "").strip()
        if preview:
            reply_parts.append(preview)
        raw_artifacts = result.output.get("artifacts")
        if isinstance(raw_artifacts, list):
            artifacts.extend([item for item in raw_artifacts if isinstance(item, dict)])
        if isinstance(result.output.get("analysis"), dict) and analysis is None:
            analysis = result.output["analysis"]
    text = "\n\n".join(reply_parts).strip()
    if failed_parts:
        failure_text = "部分步骤未完成，可稍后重试：\n" + "\n".join(f"- {item}" for item in failed_parts)
        text = "\n\n".join(part for part in (text, failure_text) if part).strip()
    if not text:
        text = "已完成处理。"
    return ReplyPackage(text=text, artifacts=artifacts, analysis=analysis)


def _failure_summary(result: WorkerResult) -> str:
    label = str(result.worker or result.step_id or "worker").strip()
    error = str(result.error or "unknown error").strip()
    return f"{label}: {error}"
