from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.core.config import settings
from app.services.graph.state import DAGPlan, PlanStep, WorkerResult, WorkflowGraphState
from app.services.graph.tools.analysis_tool_adapter import AnalysisWorkerAdapter, HelpWorkerAdapter, ReplyWorkerAdapter
from app.services.graph.tools.artifact_tool_adapter import ArtifactWorkerAdapter
from app.services.graph.tools.delivery_tool_adapter import DeliveryWorkerAdapter
from app.services.graph.tools.task_tool_adapter import TaskWorkerAdapter

logger = logging.getLogger(__name__)


def execute_workers_node(workflow: Any):
    def run(state: dict) -> dict:
        graph_state = WorkflowGraphState.model_validate(state)
        plan = graph_state.plan or DAGPlan(plan_id="empty")
        existing_results = dict(graph_state.worker_results)
        new_results = _execute_plan(workflow, graph_state, plan, existing_results)
        merged_results = {**existing_results, **new_results}
        return {
            **state,
            "worker_results": {
                key: value.model_dump(mode="json")
                for key, value in merged_results.items()
            },
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.executor",
                    "status": "done",
                    "worker_count": len(new_results),
                    "failed_count": len([item for item in new_results.values() if not item.ok]),
                },
            ],
        }

    return run


def _execute_plan(
    workflow: Any,
    state: WorkflowGraphState,
    plan: DAGPlan,
    existing_results: dict[str, WorkerResult],
) -> dict[str, WorkerResult]:
    pending = {step.step_id: step for step in plan.steps if step.step_id not in existing_results}
    completed = set(existing_results)
    results: dict[str, WorkerResult] = {}
    while pending:
        ready = [
            step
            for step in pending.values()
            if all(dependency in completed or dependency in results for dependency in step.depends_on)
        ]
        if not ready:
            for step_id, step in list(pending.items()):
                results[step_id] = WorkerResult(
                    step_id=step.step_id,
                    worker=step.worker,
                    ok=False,
                    status="failed",
                    error="unresolved or cyclic dependency",
                )
                pending.pop(step_id, None)
            break

        batch = ready if all(step.can_run_parallel for step in ready) else ready[:1]
        batch_results = _execute_batch(workflow, state, batch)
        for step in batch:
            pending.pop(step.step_id, None)
            results[step.step_id] = batch_results[step.step_id]
        completed.update(batch_results)
        state = state.model_copy(update={"worker_results": {**existing_results, **results}})
    return results


def _execute_batch(workflow: Any, state: WorkflowGraphState, steps: list[PlanStep]) -> dict[str, WorkerResult]:
    if len(steps) <= 1:
        step = steps[0]
        return {step.step_id: _execute_step(workflow, state, step)}
    max_workers = max(1, min(settings.langgraph_max_parallel_workers, len(steps)))
    logger.info(
        "LangGraph DAG batch started: step_ids=%s workers=%s max_workers=%s",
        [step.step_id for step in steps],
        [step.worker for step in steps],
        max_workers,
    )
    results: dict[str, WorkerResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="graph-worker") as executor:
        future_map = {executor.submit(_execute_step, workflow, state, step): step for step in steps}
        for future in as_completed(future_map):
            step = future_map[future]
            try:
                results[step.step_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                logger.exception("LangGraph worker step failed: step_id=%s", step.step_id)
                results[step.step_id] = WorkerResult(
                    step_id=step.step_id,
                    worker=step.worker,
                    ok=False,
                    status="failed",
                    error=str(exc),
                )
    return results


def _execute_step(workflow: Any, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
    started = time.perf_counter()
    _persist_step(workflow, state, step, status="running")
    try:
        result = _dispatch_step(workflow, state, step)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LangGraph worker dispatch failed: step_id=%s", step.step_id)
        result = WorkerResult(
            step_id=step.step_id,
            worker=step.worker,
            ok=False,
            status="failed",
            error=str(exc),
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    result = result.model_copy(update={"elapsed_ms": elapsed_ms})
    _persist_step(
        workflow,
        state,
        step,
        status=result.status,
        output_payload=result.output,
        error=result.error,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "LangGraph worker completed: step_id=%s worker=%s status=%s ok=%s elapsed_ms=%.1f",
        step.step_id,
        step.worker,
        result.status,
        result.ok,
        elapsed_ms,
    )
    return result


def _dispatch_step(workflow: Any, state: WorkflowGraphState, step: PlanStep) -> WorkerResult:
    custom_executor = getattr(workflow, "graph_worker_executor", None)
    if callable(custom_executor):
        result = custom_executor(state, step)
        if isinstance(result, WorkerResult):
            return result
        if isinstance(result, dict):
            return WorkerResult.model_validate(result)
    if step.worker == "task":
        return TaskWorkerAdapter(workflow).run(state, step)
    if step.worker == "analysis":
        return AnalysisWorkerAdapter(workflow).run(state, step)
    if step.worker == "help":
        return HelpWorkerAdapter(workflow).run(state, step)
    if step.worker == "reply":
        return ReplyWorkerAdapter(workflow).run(state, step)
    if step.worker in {"doc", "slides", "canvas"}:
        return ArtifactWorkerAdapter(workflow).run(state, step)
    if step.worker == "delivery":
        return DeliveryWorkerAdapter(workflow).run(state, step)
    return WorkerResult(
        step_id=step.step_id,
        worker=step.worker,
        ok=False,
        status="failed",
        error=f"unsupported graph worker: {step.worker}",
    )


def _persist_step(
    workflow: Any,
    state: WorkflowGraphState,
    step: PlanStep,
    *,
    status: str,
    output_payload: dict[str, Any] | None = None,
    error: str | None = None,
    elapsed_ms: float | None = None,
) -> None:
    task_run_id = state.task_run_id
    task_run_service = getattr(workflow, "task_run_service", None)
    if not task_run_id or task_run_service is None:
        return
    payload = {
        "step_id": step.step_id,
        "worker": step.worker,
        "operation": step.operation,
        "depends_on": step.depends_on,
        "idempotency_key": step.idempotency_key,
    }
    if output_payload:
        payload["output"] = output_payload
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    task_run_service.upsert_step(
        task_run_id,
        step_key=f"graph.worker.{step.step_id}",
        title=f"LangGraph worker {step.worker}",
        step_type="graph_worker",
        status=status,
        output_payload=payload,
        error=error,
    )
