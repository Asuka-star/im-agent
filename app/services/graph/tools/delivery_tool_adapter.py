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
        if not manifest.get("artifacts"):
            return WorkerResult(
                step_id=step.step_id,
                worker=step.worker,
                ok=False,
                status="needs_review",
                error="No openable artifact links are available for delivery.",
                output={
                    "clarification": {
                        "question": "当前需求下还没有可打开的文档、PPT 或 Canvas 链接，要先生成一个产物吗？",
                        "reason": "交付包清单只汇总已有链接，不重新生成内容。",
                        "options": ["先生成需求文档", "先生成答辩 PPT", "先画产品流程图"],
                        "blocking": True,
                    }
                },
            )
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
                "reply_preview": f"已生成交付包清单，汇总 {len(artifact.get('preview', {}).get('artifacts', []))} 个最新产物链接。",
                "artifacts": [artifact],
                "delivery_artifact_count": len(artifact.get("preview", {}).get("artifacts", [])),
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
        documents = list(state.context.source_docs or [])
        if isinstance(state.context.current_document, dict):
            documents.append(state.context.current_document)
        for document in documents:
            if not isinstance(document, dict):
                continue
            document_id = str(document.get("document_id") or "").strip()
            if not document_id:
                continue
            add_item(
                {
                    "artifact_id": document_id,
                    "artifact_type": "document",
                    "title": str(document.get("title") or "协作文档"),
                    "status": "ready",
                    "provider": "feishu" if document.get("url") else "local",
                    "url": document.get("url"),
                    "version": document.get("version") or 1,
                }
            )

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
    title = state.message.text.strip()[:80] or "需求交付清单"
    deliverables = _latest_deliverables(artifacts)
    ready_items = [item for item in deliverables if item.get("status") == "ready"]
    missing_labels = [str(item["label"]) for item in deliverables if item.get("status") != "ready"]
    return {
        "title": f"需求交付清单 - {title}",
        "task_run_id": state.task_run_id,
        "session_id": state.message.session_id,
        "summary": f"已汇总当前需求下最新产物链接：{len(ready_items)}/3 项已生成。",
        "source": {
            "source_type": state.message.chat_type,
            "source_ref": state.message.chat_id,
            "trigger_message_id": state.message.message_id,
            "requested_by": "langgraph",
        },
        "checks": [
            {
                "key": str(item["key"]),
                "label": str(item["label"]),
                "status": str(item["status"]),
                "category": "delivery",
                "detail": str(item.get("detail") or ""),
            }
            for item in deliverables
        ],
        "artifacts": [_artifact_item(item) for item in ready_items],
        "artifact_summaries": deliverables,
        "deliverables": deliverables,
        "highlights": [
            f"已生成：{len(ready_items)} 项；待补齐：{len(missing_labels)} 项。",
            "本清单只汇总当前需求下最新的文档、PPT 和 Canvas 链接，不重新生成内容。",
        ],
        "next_steps": [f"建议补齐：{'、'.join(missing_labels)}。"] if missing_labels else ["可将该清单作为当前需求的最终归档入口。"],
    }


def _latest_deliverables(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _deliverable_item(
            key="document",
            label="需求文档",
            artifact_type="document",
            item=_latest_artifact(artifacts, {"document", "doc", "feishu_doc"}),
            missing_detail="当前需求下还没有需求文档链接。",
        ),
        _deliverable_item(
            key="slides",
            label="答辩 PPT",
            artifact_type="slides_package",
            item=_latest_artifact(artifacts, {"slides", "slides_package"}),
            missing_detail="当前需求下还没有 PPT 链接。",
        ),
        _deliverable_item(
            key="canvas",
            label="Canvas / 流程图",
            artifact_type="canvas",
            item=_latest_artifact(artifacts, {"canvas"}),
            missing_detail="当前需求下还没有 Canvas 链接。",
        ),
    ]


def _latest_artifact(artifacts: list[dict[str, Any]], artifact_types: set[str]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in artifacts
        if str(item.get("artifact_type") or "").strip() in artifact_types
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), int(item.get("version") or 1)))


def _deliverable_item(
    *,
    key: str,
    label: str,
    artifact_type: str,
    item: dict[str, Any] | None,
    missing_detail: str,
) -> dict[str, Any]:
    if item is None:
        return {
            "key": key,
            "label": label,
            "artifact_type": artifact_type,
            "title": label,
            "status": "missing",
            "detail": missing_detail,
            "url": None,
            "links": [],
        }
    title = str(item.get("title") or item.get("artifact_id") or label).strip() or label
    url = str(item.get("url") or "").strip() or None
    links = _links_for_item(item, primary_url=url)
    status = "ready" if links or url else "partial"
    detail = f"最新{label}：{title}" if status == "ready" else f"已记录{label}，但缺少可打开链接。"
    return {
        "key": key,
        "label": label,
        "artifact_id": item.get("artifact_id"),
        "artifact_type": str(item.get("artifact_type") or artifact_type),
        "title": title,
        "status": status,
        "provider": item.get("provider") or ("feishu" if key == "document" else "local"),
        "url": url,
        "version": item.get("version") or 1,
        "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
        "detail": detail,
        "links": links,
    }


def _links_for_item(item: dict[str, Any], *, primary_url: str | None) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    if primary_url:
        links.append({"label": "打开", "url": primary_url})
    preview = item.get("preview") if isinstance(item.get("preview"), dict) else {}
    exports = preview.get("exports") if isinstance(preview.get("exports"), dict) else {}
    for key, value in exports.items():
        url = str(value or "").strip()
        if url and all(existing["url"] != url for existing in links):
            links.append({"label": str(key).upper(), "url": url})
    return links


def _artifact_item(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "artifact_type": artifact.get("artifact_type"),
        "title": artifact.get("title") or "协作产物",
        "status": artifact.get("status") or "ready",
        "provider": artifact.get("provider") or "local",
        "url": artifact.get("url"),
        "version": artifact.get("version") or 1,
        "links": artifact.get("links") if isinstance(artifact.get("links"), list) else [],
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
