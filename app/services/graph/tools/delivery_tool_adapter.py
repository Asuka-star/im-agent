from __future__ import annotations

from typing import Any

from app.services.graph.state import PlanStep, WorkerResult, WorkflowGraphState


class DeliveryWorkerAdapter:
    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def run(self, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
        artifacts = _collect_artifacts(state)
        if not artifacts:
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=False,
                status="needs_review",
                error="No graph artifacts are available to bundle for delivery.",
                output={
                    "clarification": {
                        "question": "当前还没有可打包的产物，要先生成文档、PPT 或画布吗？",
                        "reason": "交付包需要至少一个可归档的协作产物。",
                        "options": ["先生成文档", "先生成 PPT", "先生成画布"],
                        "blocking": True,
                    }
                },
            )
        service = getattr(self.workflow, "delivery_artifact_service", None)
        if service is None or not callable(getattr(service, "persist_bundle", None)):
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=False,
                status="failed",
                error="delivery artifact service unavailable",
            )
        manifest = _manifest_for_state(state, artifacts)
        artifact = service.persist_bundle(
            manifest,
            task_run_id=state.task_run_id or state.message.message_id or state.message.session_id,
            session_id=state.message.session_id,
        )
        return WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=True,
            output={
                "mode": "delivery",
                "reply_preview": f"已生成交付包，包含 {len(artifacts)} 个产物。",
                "artifacts": [artifact],
                "delivery_artifact_count": len(artifacts),
                "close_title": "delivery",
            },
        )


def _collect_artifacts(state: WorkflowGraphState) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_item(item: dict[str, Any]) -> None:
        artifact_type = str(item.get("artifact_type") or "").strip()
        if not artifact_type or artifact_type == "delivery_bundle":
            return
        key = str(item.get("artifact_id") or item.get("url") or item.get("title") or id(item))
        if key in seen:
            return
        seen.add(key)
        artifacts.append(item)

    if state.context is not None:
        for item in state.context.artifacts:
            if isinstance(item, dict):
                add_item(item)

    for result in state.worker_results.values():
        if not result.ok:
            continue
        raw_items = result.output.get("artifacts") if isinstance(result.output, dict) else None
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            add_item(item)
    return artifacts


def _manifest_for_state(state: WorkflowGraphState, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    title = state.message.text.strip()[:80] or "LangGraph 交付包"
    return {
        "title": f"任务交付包 - {title}",
        "task_run_id": state.task_run_id,
        "session_id": state.message.session_id,
        "summary": f"LangGraph 已汇总 {len(artifacts)} 个协作产物。",
        "source": {
            "source_type": state.message.chat_type,
            "source_ref": state.message.chat_id,
            "trigger_message_id": state.message.message_id,
            "requested_by": "langgraph",
        },
        "checks": [
            {
                "label": "产物已生成",
                "status": "ready",
                "category": "delivery",
                "detail": f"{len(artifacts)} 个产物已纳入交付包。",
            }
        ],
        "artifacts": [_artifact_item(item) for item in artifacts],
        "artifact_summaries": [_artifact_summary(item) for item in artifacts],
        "highlights": [f"已纳入 {len(artifacts)} 个图执行产物。"],
        "next_steps": ["把交付包链接回发到 IM 会话，作为本轮协作归档入口。"],
    }


def _artifact_item(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "artifact_type": artifact.get("artifact_type"),
        "title": artifact.get("title") or "协作产物",
        "status": artifact.get("status") or "ready",
        "provider": artifact.get("provider") or "local",
        "url": artifact.get("url"),
        "version": artifact.get("version") or 1,
    }


def _artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "artifact_type": artifact.get("artifact_type"),
        "title": artifact.get("title") or "协作产物",
        "status": artifact.get("status") or "ready",
        "url": artifact.get("url"),
        "version": artifact.get("version") or 1,
    }
