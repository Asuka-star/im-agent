from __future__ import annotations

from app.services.delivery_artifact_service import DeliveryArtifactService
from app.services.task_run_service import TaskRunService
from app.utils.values import coerce_positive_int


class DeliveryTool:
    """Builds and persists task delivery bundles for final handoff."""

    def __init__(
        self,
        *,
        delivery_artifact_service: DeliveryArtifactService,
        task_run_service: TaskRunService,
    ) -> None:
        self.delivery_artifact_service = delivery_artifact_service
        self.task_run_service = task_run_service

    def bundle_from_task_run(self, task_run_id: str, *, requested_by: str = "pilot_workbench"):
        detail = self.task_run_service.get_task_run(task_run_id)
        if detail is None:
            return None

        manifest = self.build_manifest(detail, requested_by=requested_by)
        artifact = self.delivery_artifact_service.persist_bundle(
            manifest,
            task_run_id=task_run_id,
            session_id=detail.session_id,
        )
        self.task_run_service.upsert_step(
            task_run_id,
            step_key="delivery_bundle",
            title="生成交付包",
            step_type="artifact",
            status="done",
            input_payload={"requested_by": requested_by},
            output_payload={
                "url": artifact.get("url"),
                "artifact_count": len(artifact.get("preview", {}).get("artifacts", [])),
                "ready_checks": self._count_checks(artifact.get("preview", {}), "ready"),
            },
        )
        self.task_run_service.create_artifact(
            task_run_id,
            artifact_type=str(artifact.get("artifact_type") or "delivery_bundle"),
            title=str(artifact.get("title") or "任务交付包"),
            provider=str(artifact.get("provider") or "local"),
            status=str(artifact.get("status") or "ready"),
            url=str(artifact.get("url") or "").strip() or None,
            preview=artifact.get("preview") if isinstance(artifact.get("preview"), dict) else None,
            version=coerce_positive_int(artifact.get("version")),
        )
        update_kwargs = {"latest_summary": str(manifest.get("summary") or "交付包已生成")}
        if getattr(detail, "status", "") == "completed":
            update_kwargs["stage"] = "delivered"
            update_kwargs["status"] = "completed"
        self.task_run_service.update_task_run(task_run_id, **update_kwargs)
        return self.task_run_service.get_task_run(task_run_id)

    def build_manifest(self, detail, *, requested_by: str) -> dict:
        artifacts = [self._artifact_item(item) for item in getattr(detail, "artifacts", [])]
        documents = getattr(detail, "session_documents", []) or []
        if documents and not any(item.get("artifact_type") == "document" for item in artifacts):
            for document in documents:
                artifacts.append(
                    {
                        "artifact_id": _field(document, "document_id"),
                        "artifact_type": "document",
                        "title": _field(document, "title") or "协作文档",
                        "status": "ready",
                        "provider": "feishu" if _field(document, "url") else "local",
                        "url": _field(document, "url"),
                        "version": _field(document, "version") or 1,
                    }
                )
        artifacts = [item for item in artifacts if item.get("artifact_type") != "delivery_bundle"]
        checks = self._checks(detail, artifacts)
        ready_count = sum(1 for item in checks if item.get("status") == "ready")
        summary = (
            f"本次任务已整理 {len(artifacts)} 个交付物，"
            f"{ready_count}/{len(checks)} 个验收项满足。"
        )
        return {
            "title": f"任务交付包 · {getattr(detail, 'title', '') or getattr(detail, 'task_run_id', '')}",
            "task_run_id": getattr(detail, "task_run_id", ""),
            "session_id": getattr(detail, "session_id", ""),
            "summary": summary,
            "source": {
                "source_type": getattr(detail, "source_type", ""),
                "source_ref": getattr(detail, "source_ref", None),
                "trigger_message_id": getattr(detail, "trigger_message_id", None),
                "requested_by": requested_by,
            },
            "checks": checks,
            "artifacts": artifacts,
            "next_steps": self._next_steps(checks, artifacts),
        }

    def _artifact_item(self, artifact) -> dict:
        return {
            "artifact_id": _field(artifact, "artifact_id"),
            "artifact_type": _field(artifact, "artifact_type"),
            "title": _field(artifact, "title") or "协作产物",
            "status": _field(artifact, "status") or "ready",
            "provider": _field(artifact, "provider") or "local",
            "url": _field(artifact, "url"),
            "version": _field(artifact, "version") or 1,
        }

    def _checks(self, detail, artifacts: list[dict]) -> list[dict]:
        artifact_types = {str(item.get("artifact_type") or "") for item in artifacts}
        pending_confirmations = [
            item
            for item in (getattr(detail, "confirmations", []) or [])
            if _field(item, "status") not in {"answered", "resolved"}
        ]
        has_url = any(str(item.get("url") or "").strip() for item in artifacts)
        step_count = len(getattr(detail, "steps", []) or [])
        return [
            {
                "key": "im_entry",
                "label": "IM 入口",
                "status": "ready" if getattr(detail, "source_type", "") else "missing",
                "detail": getattr(detail, "trigger_message_id", None) or getattr(detail, "source_ref", None) or "工作台触发",
            },
            {
                "key": "agent_plan",
                "label": "Agent 编排轨迹",
                "status": "ready" if step_count else "missing",
                "detail": f"记录 {step_count} 个执行步骤",
            },
            {
                "key": "document",
                "label": "文档产物",
                "status": "ready" if "document" in artifact_types else "missing",
                "detail": "已包含协作文档" if "document" in artifact_types else "尚未生成文档产物",
            },
            {
                "key": "presentation_or_canvas",
                "label": "演示稿/画布",
                "status": "ready" if ({"slides_package", "canvas"} & artifact_types) else "missing",
                "detail": "已包含演示稿或自由画布" if ({"slides_package", "canvas"} & artifact_types) else "尚未生成演示稿或画布",
            },
            {
                "key": "confirmation",
                "label": "人工确认",
                "status": "ready" if not pending_confirmations else "partial",
                "detail": "没有待处理确认" if not pending_confirmations else f"仍有 {len(pending_confirmations)} 个待确认节点",
            },
            {
                "key": "shareable_links",
                "label": "可分享链接",
                "status": "ready" if has_url else "partial",
                "detail": "至少一个产物可直接打开" if has_url else "当前只有结构化预览，缺少外部链接",
            },
        ]

    def _next_steps(self, checks: list[dict], artifacts: list[dict]) -> list[str]:
        next_steps = ["把交付包链接贴回 IM 会话，作为本轮讨论的归档入口。"]
        missing = [str(item.get("label")) for item in checks if item.get("status") == "missing"]
        if missing:
            next_steps.append("补齐：" + "、".join(missing))
        if any(item.get("artifact_type") == "slides_package" for item in artifacts):
            next_steps.append("进入排练模式检查讲者备注，并按评委视角做最后一轮修订。")
        return next_steps

    def _count_checks(self, manifest: dict, status: str) -> int:
        checks = manifest.get("checks") if isinstance(manifest, dict) else []
        if not isinstance(checks, list):
            return 0
        return sum(1 for item in checks if isinstance(item, dict) and item.get("status") == status)


def _field(item: object, field: str):
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)
