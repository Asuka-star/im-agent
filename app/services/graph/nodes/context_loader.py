from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.services.graph.state import ContextPack, WorkspaceCommand

logger = logging.getLogger(__name__)


def context_loader_node(workflow: Any):
    def run(state: dict) -> dict:
        message = state.get("message") or {}
        context = ContextPack.model_validate(state.get("context") or {"session_id": message.get("session_id") or ""})
        session_id = str(message.get("session_id") or context.session_id)
        started = time.perf_counter()
        loaded_values = _load_sources(workflow, state=state, message=message, context=context, session_id=session_id)

        loaded = context.model_copy(
            update={
                "session_id": session_id,
                "tasks": loaded_values["tasks"],
                "current_document": loaded_values["current_document"],
                "recent_messages": loaded_values["recent_messages"],
                "artifacts": loaded_values["artifacts"],
                "source_docs": loaded_values["source_docs"],
                "output_requirements": loaded_values["output_requirements"],
                "missing_fields": loaded_values["missing_fields"],
                "loaded_sources": loaded_values["source_traces"],
                "errors": loaded_values["errors"],
            }
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            **state,
            "context": loaded.model_dump(mode="json"),
            "trace": [
                *list(state.get("trace") or []),
                {
                    "node": "graph.context_loader",
                    "status": "done",
                    "task_count": len(loaded.tasks),
                    "recent_message_count": len(loaded.recent_messages),
                    "artifact_count": len(loaded.artifacts),
                    "has_current_document": loaded.current_document is not None,
                    "elapsed_ms": elapsed_ms,
                    "sources": loaded_values["source_traces"],
                },
            ],
        }

    return run


def _load_sources(
    workflow: Any,
    *,
    state: dict,
    message: dict[str, Any],
    context: ContextPack,
    session_id: str,
) -> dict[str, Any]:
    loaders: dict[str, Callable[[], Any]] = {
        "tasks": lambda: _load_tasks(workflow, message),
        "recent_messages": lambda: _load_recent_messages(workflow, state, message, session_id),
    }
    if context.current_document is None:
        loaders["current_document"] = lambda: _load_current_document(workflow, session_id)
    artifacts_loader = getattr(workflow, "graph_context_artifacts_loader", None)
    if callable(artifacts_loader):
        loaders["artifacts"] = lambda: [_model_dump(item) for item in artifacts_loader(session_id, state.get("task_run_id")) or []]

    values: dict[str, Any] = {
        "tasks": list(context.tasks),
        "current_document": context.current_document,
        "recent_messages": list(context.recent_messages),
        "artifacts": list(context.artifacts),
    }
    errors = list(context.errors)
    source_traces: list[dict[str, Any]] = []
    max_workers = max(1, min(settings.langgraph_max_parallel_workers, len(loaders)))
    timeout_seconds = max(0.1, float(settings.langgraph_node_timeout_seconds or 1.0))
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="graph-context")
    future_map = {executor.submit(_timed_load, field, loader): field for field, loader in loaders.items()}
    done, pending = wait(future_map, timeout=timeout_seconds)
    for future in done:
        field = future_map[future]
        try:
            loaded = future.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangGraph context loader skipped %s: %s", field, exc)
            errors.append({"node": "graph.context_loader", "field": field, "error": str(exc)})
            source_traces.append({"field": field, "status": "failed", "error": str(exc)})
            continue
        values[field] = loaded["value"]
        source_traces.append(
            {
                "field": field,
                "status": "done",
                "elapsed_ms": loaded["elapsed_ms"],
                "count": _source_count(loaded["value"]),
            }
        )
    for future in pending:
        field = future_map[future]
        future.cancel()
        error = f"context source timed out after {timeout_seconds:.1f}s"
        errors.append({"node": "graph.context_loader", "field": field, "error": error})
        source_traces.append({"field": field, "status": "timeout", "error": error})
    executor.shutdown(wait=False, cancel_futures=True)
    command = WorkspaceCommand.model_validate(state.get("command") or {})
    source_docs = _source_docs(values["current_document"], context.source_docs)
    output_requirements = _output_requirements(command, context)
    missing_fields = _missing_fields(command, context, values)
    return {
        **values,
        "source_docs": source_docs,
        "output_requirements": output_requirements,
        "missing_fields": missing_fields,
        "errors": errors,
        "source_traces": sorted(source_traces, key=lambda item: str(item.get("field") or "")),
    }


def _timed_load(field: str, loader: Callable[[], Any]) -> dict[str, Any]:
    started = time.perf_counter()
    value = loader()
    return {
        "field": field,
        "value": value,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    }


def _load_tasks(workflow: Any, message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tasks = workflow._context_tasks_for_message(_message_proxy(message))
    return [_model_dump(item) for item in raw_tasks]


def _load_current_document(workflow: Any, session_id: str) -> dict[str, Any] | None:
    session_document_service = getattr(workflow, "session_document_service", None)
    get_current_document = getattr(session_document_service, "get_current_document", None)
    if not callable(get_current_document):
        return None
    document = get_current_document(session_id)
    return document if isinstance(document, dict) else None


def _load_recent_messages(
    workflow: Any,
    state: dict[str, Any],
    message: dict[str, Any],
    session_id: str,
) -> list[dict[str, Any]]:
    memory_service = getattr(workflow, "memory_service", None)
    get_episode_messages = getattr(memory_service, "get_episode_messages", None)
    active_episode_id = state.get("active_episode_id")
    if not callable(get_episode_messages) or active_episode_id is None:
        return []
    messages = get_episode_messages(
        session_id,
        episode_id=active_episode_id,
        exclude_message_id=message.get("message_id"),
        limit=10,
    )
    return [
        {
            "sender_id": getattr(item, "sender_id", None),
            "content": str(getattr(item, "content", "") or "")[:500],
        }
        for item in messages or []
    ]


def _source_count(value: Any) -> int:
    if isinstance(value, list | tuple | dict):
        return len(value)
    return 1 if value is not None else 0


def _source_docs(current_document: dict[str, Any] | None, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = [dict(item) for item in existing if isinstance(item, dict)]
    if isinstance(current_document, dict):
        document_id = str(current_document.get("document_id") or "").strip()
        if document_id and not any(str(item.get("document_id") or "") == document_id for item in docs):
            docs.append(
                {
                    "document_id": document_id,
                    "title": str(current_document.get("title") or "").strip(),
                    "url": current_document.get("url"),
                    "version": current_document.get("version"),
                    "is_current": True,
                }
            )
    return docs


def _output_requirements(command: WorkspaceCommand, context: ContextPack) -> dict[str, Any]:
    existing = dict(context.output_requirements)
    outputs = _requested_outputs(command, context)
    if outputs:
        existing["requested_outputs"] = outputs
    if command.artifact_goals:
        existing["artifact_goals"] = dict(command.artifact_goals)
    if command.operation:
        existing["operation"] = command.operation
    if command.object:
        existing["object"] = command.object
    return existing


def _requested_outputs(command: WorkspaceCommand, context: ContextPack) -> list[str]:
    outputs: list[str] = []
    raw_outputs = context.output_requirements.get("requested_outputs") if isinstance(context.output_requirements, dict) else []
    candidates = raw_outputs if isinstance(raw_outputs, list | tuple) else [raw_outputs] if isinstance(raw_outputs, str) else []
    candidates = [*candidates, *command.requested_outputs]
    legacy_route = context.legacy_route if isinstance(context.legacy_route, dict) else {}
    legacy_outputs = legacy_route.get("requested_outputs")
    if isinstance(legacy_outputs, list | tuple):
        candidates.extend(legacy_outputs)
    elif isinstance(legacy_outputs, str):
        candidates.append(legacy_outputs)
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
    for item in candidates:
        output = aliases.get(str(item or "").strip().lower(), str(item or "").strip().lower())
        if output in {"doc", "slides", "canvas"} and output not in outputs:
            outputs.append(output)
    return outputs


def _missing_fields(command: WorkspaceCommand, context: ContextPack, values: dict[str, Any]) -> list[dict[str, Any]]:
    managed_fields = {"requested_outputs", "current_document", "tasks", "slides_artifact", "canvas_artifact"}
    missing = [
        dict(item)
        for item in context.missing_fields
        if isinstance(item, dict) and str(item.get("field") or "") not in managed_fields
    ]
    seen = {str(item.get("field") or "") for item in missing}

    def add(field: str, reason: str, *, severity: str = "blocking") -> None:
        if field in seen:
            return
        missing.append({"field": field, "reason": reason, "severity": severity})
        seen.add(field)

    if command.operation == "generate" and not _requested_outputs(command, context) and command.route != "delivery" and command.object != "delivery":
        add("requested_outputs", "生成类请求缺少明确的产物类型。")
    if command.object == "doc" and command.operation in {"revise", "update"} and values.get("current_document") is None:
        add("current_document", "文档修订需要先锁定目标文档。")
    if command.object in {"task", "tasks"} and command.operation in {"remove", "complete", "assign"} and not values.get("tasks"):
        add("tasks", "任务写操作需要当前任务快照。")
    if command.object in {"slides", "canvas"} and command.operation in {"revise", "update"}:
        artifacts = values.get("artifacts") if isinstance(values.get("artifacts"), list) else []
        if command.object == "slides" and not any(str(item.get("artifact_type") or "") in {"slides", "slides_package"} for item in artifacts if isinstance(item, dict)):
            add("slides_artifact", "PPT 修订需要已有演示稿产物。")
        if command.object == "canvas" and not any(str(item.get("artifact_type") or "") == "canvas" for item in artifacts if isinstance(item, dict)):
            add("canvas_artifact", "画布修订需要已有画布产物。")
    return missing


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return {
        key: getattr(value, key)
        for key in ("title", "owner", "priority", "due_date", "status", "notes")
        if hasattr(value, key)
    }


class _message_proxy:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.__dict__.update(payload)
