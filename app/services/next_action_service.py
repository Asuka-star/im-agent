from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas.next_action import NextActionBundle, NextActionRecommendation
from app.schemas.task_run import ArtifactRecord, SessionDocumentRecord, TaskRunDetail

logger = logging.getLogger(__name__)


class ContextualNextActionService:
    """Builds safe, contextual next-action recommendations for task runs."""

    def __init__(self, *, llm_service: Any | None = None, enable_llm: bool = False) -> None:
        self.llm_service = llm_service
        self.enable_llm = enable_llm

    def build_for_task_run(
        self,
        detail: TaskRunDetail,
        *,
        recent_context: str | None = None,
        max_items: int = 3,
    ) -> NextActionBundle:
        signals = self.collect_signals(detail, recent_context=recent_context)
        candidates = self.generate_candidates(signals)
        recommendations = self.rank_and_trim(candidates, max_items=max_items)
        recommendations = self._maybe_rerank_with_llm(signals, recommendations, max_items=max_items)
        return NextActionBundle(
            task_run_id=detail.task_run_id,
            session_id=detail.session_id,
            summary=self._summary(signals, recommendations),
            recommendations=recommendations,
        )

    def collect_signals(self, detail: TaskRunDetail, *, recent_context: str | None = None) -> dict[str, Any]:
        artifact_types = {self._artifact_type(item) for item in detail.artifacts}
        current_document = self._current_document(detail.session_documents)
        pending_confirmations = [
            item for item in detail.confirmations if str(item.status or "").lower() in {"pending", "open"}
        ]
        failed_steps = [
            item for item in detail.steps if str(item.status or "").lower() == "failed" or str(item.error or "").strip()
        ]
        latest_text = "\n".join(
            item
            for item in (
                detail.latest_summary,
                detail.latest_reply_preview,
                detail.latest_error,
                recent_context,
            )
            if item
        )
        has_doc = bool({"document", "doc"} & artifact_types) or bool(detail.session_documents)
        has_slides = bool({"slides", "slides_package"} & artifact_types)
        has_canvas = "canvas" in artifact_types
        has_delivery = "delivery_bundle" in artifact_types
        sync_failed = self._contains_any(
            latest_text,
            ("Document sync failed", "sync failed", "同步失败", "同步失败"),
        ) or any("sync" in str(item.error or "").lower() for item in failed_steps)
        local_only_doc = any(
            self._artifact_type(item) in {"document", "doc"}
            and (self._artifact_provider(item) == "local" or not str(item.url or "").strip())
            for item in detail.artifacts
        )
        return {
            "task_run_id": detail.task_run_id,
            "session_id": detail.session_id,
            "status": detail.status,
            "stage": detail.stage,
            "intent": detail.intent or "",
            "latest_error": detail.latest_error or "",
            "latest_reply_preview": detail.latest_reply_preview or "",
            "recent_context": recent_context or "",
            "artifact_types": artifact_types,
            "has_doc": has_doc,
            "has_slides": has_slides,
            "has_canvas": has_canvas,
            "has_delivery": has_delivery,
            "sync_failed": sync_failed,
            "local_only_doc": local_only_doc,
            "pending_confirmations": pending_confirmations,
            "current_document": current_document,
            "document_count": len(detail.session_documents),
        }

    def generate_candidates(self, signals: dict[str, Any]) -> list[NextActionRecommendation]:
        candidates: list[NextActionRecommendation] = []
        pending_confirmations = signals.get("pending_confirmations") or []
        if signals.get("status") == "waiting_confirmation" or pending_confirmations:
            confirmation = pending_confirmations[0] if pending_confirmations else None
            prompt = str(getattr(confirmation, "prompt", "") or "当前任务需要你先确认一个选项。")
            candidates.append(
                self._recommendation(
                    "answer_confirmation",
                    "先回答当前确认问题",
                    action_type="answer_confirmation",
                    priority="high",
                    confidence=0.98,
                    reason=f"任务正在等待确认：{prompt}",
                    command=prompt,
                    target_kind="confirmation",
                    target_id=str(getattr(confirmation, "confirmation_id", "") or "") or None,
                    score=100,
                )
            )

        if signals.get("sync_failed") or signals.get("local_only_doc"):
            candidates.append(
                self._recommendation(
                    "retry_sync",
                    "重试同步到飞书文档",
                    action_type="retry_sync",
                    priority="high",
                    confidence=0.92,
                    reason="最近一次文档同步失败或只生成了本地产物，先恢复同步比继续分享更稳。",
                    command="请重试同步到飞书文档",
                    target_kind="document",
                    score=95,
                )
            )

        if signals.get("has_doc") and not signals.get("has_slides"):
            candidates.append(
                self._recommendation(
                    "generate_slides",
                    "基于当前文档生成汇报 PPT",
                    action_type="generate_slides",
                    priority="high",
                    confidence=0.88,
                    reason="当前已经有协作文档，但还没有演示稿产物，适合继续推进到汇报材料。",
                    command="基于当前文档生成 5 页汇报 PPT",
                    target_kind="document",
                    target_id=self._document_id(signals.get("current_document")),
                    score=80,
                )
            )
        if signals.get("has_doc") and not signals.get("has_canvas"):
            candidates.append(
                self._recommendation(
                    "generate_canvas",
                    "把流程和风险整理成自由画布",
                    action_type="generate_canvas",
                    priority="medium",
                    confidence=0.78,
                    reason="文档内容已经沉淀，可以转成画布帮助团队快速看清流程、依赖和风险。",
                    command="基于当前文档生成自由画布",
                    target_kind="document",
                    target_id=self._document_id(signals.get("current_document")),
                    score=70,
                )
            )

        if signals.get("has_doc") and (signals.get("has_slides") or signals.get("has_canvas")) and not signals.get("has_delivery"):
            candidates.append(
                self._recommendation(
                    "bundle_delivery",
                    "生成最终归档清单",
                    action_type="bundle_delivery",
                    priority="medium",
                    confidence=0.72,
                    reason="当前已经具备文档和至少一种展示产物，可以在最终归档时生成一个统一入口；日常协作中不必提前生成。",
                    command="生成最终归档清单",
                    score=52,
                )
            )
        if signals.get("has_doc") and signals.get("has_slides") and not signals.get("has_delivery"):
            candidates.append(
                self._recommendation(
                    "revise_slides",
                    "检查演示重点和讲者备注",
                    action_type="revise_slides",
                    priority="medium",
                    confidence=0.68,
                    reason="PPT 已经生成，排练前可以先按评委视角做一轮精修。",
                    command="帮我检查演示重点和讲者备注",
                    target_kind="slides",
                    score=55,
                )
            )

        if signals.get("document_count", 0) > 1 and signals.get("intent") == "doc":
            candidates.append(
                self._recommendation(
                    "select_document",
                    "选择要继续修订的文档",
                    action_type="ask_clarification",
                    priority="medium",
                    confidence=0.74,
                    reason="当前会话下有多份文档，继续修订前最好先确认目标。",
                    command="请列出可修订文档，我来选择目标",
                    target_kind="document",
                    score=65,
                )
            )

        if signals.get("has_doc") and (signals.get("has_slides") or signals.get("has_canvas")) and signals.get("has_delivery"):
            candidates.append(
                self._recommendation(
                    "share_to_im",
                    "把本轮成果链接回发到 IM",
                    action_type="share_to_im",
                    priority="medium",
                    confidence=0.76,
                    reason="核心产物已经齐备，可以把交付入口发回群聊完成归档。",
                    command="把本轮成果链接发回 IM",
                    score=60,
                )
            )

        return candidates

    def rank_and_trim(self, candidates: list[NextActionRecommendation], *, max_items: int = 3) -> list[NextActionRecommendation]:
        if max_items <= 0:
            return []
        seen: set[str] = set()
        unique: list[NextActionRecommendation] = []
        for candidate in sorted(candidates, key=lambda item: item.metadata.get("score", 0), reverse=True):
            if candidate.action_type in seen:
                continue
            seen.add(candidate.action_type)
            unique.append(candidate)
            if len(unique) >= max_items:
                break
        return unique

    def _maybe_rerank_with_llm(
        self,
        signals: dict[str, Any],
        recommendations: list[NextActionRecommendation],
        *,
        max_items: int,
    ) -> list[NextActionRecommendation]:
        if not self.enable_llm or not recommendations or self.llm_service is None:
            return recommendations
        if not getattr(self.llm_service, "is_configured", lambda: False)():
            return recommendations
        try:
            result = self.llm_service.rerank_next_actions(
                {
                    "signals": self._jsonable_signals(signals),
                    "recommendations": [item.model_dump(mode="json") for item in recommendations],
                    "max_items": max_items,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM next-action rerank failed, using rule order: %s", exc)
            return recommendations
        return self._apply_llm_rerank(result, recommendations, max_items=max_items)

    def _apply_llm_rerank(
        self,
        result: dict[str, Any],
        recommendations: list[NextActionRecommendation],
        *,
        max_items: int,
    ) -> list[NextActionRecommendation]:
        by_id = {item.action_id: item for item in recommendations}
        ordered_ids = result.get("order") if isinstance(result.get("order"), list) else []
        overrides = result.get("recommendations") if isinstance(result.get("recommendations"), list) else []
        for override in overrides:
            if not isinstance(override, dict):
                continue
            action_id = str(override.get("action_id") or "").strip()
            current = by_id.get(action_id)
            if current is None:
                continue
            patch = {
                key: override[key]
                for key in ("title", "description", "reason", "command")
                if isinstance(override.get(key), str) and str(override.get(key)).strip()
            }
            if patch:
                by_id[action_id] = current.model_copy(update={**patch, "source": "hybrid"})
        ordered: list[NextActionRecommendation] = []
        for action_id in ordered_ids:
            item = by_id.get(str(action_id))
            if item is not None and item not in ordered:
                ordered.append(item)
        for item in recommendations:
            candidate = by_id[item.action_id]
            if candidate not in ordered:
                ordered.append(candidate)
        return ordered[:max_items]

    def _summary(self, signals: dict[str, Any], recommendations: list[NextActionRecommendation]) -> str:
        if not recommendations:
            return "当前上下文里没有足够明确的下一步建议。"
        if signals.get("status") == "waiting_confirmation":
            return "当前协作运行正在等待用户确认。"
        if signals.get("sync_failed"):
            return "当前优先建议恢复文档同步。"
        return "已根据当前协作状态和产物缺口生成下一步建议。"

    def _recommendation(
        self,
        action_id: str,
        title: str,
        *,
        action_type: str,
        priority: str,
        confidence: float,
        reason: str,
        command: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        score: int,
    ) -> NextActionRecommendation:
        return NextActionRecommendation(
            action_id=action_id,
            title=title,
            action_type=action_type,
            priority=priority,
            confidence=confidence,
            reason=reason,
            command=command,
            target_kind=target_kind,
            target_id=target_id,
            metadata={"score": score},
        )

    @staticmethod
    def _artifact_type(artifact: ArtifactRecord) -> str:
        return str(getattr(artifact, "artifact_type", "") or "").strip()

    @staticmethod
    def _artifact_provider(artifact: ArtifactRecord) -> str:
        return str(getattr(artifact, "provider", "") or "").strip()

    @staticmethod
    def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(marker.lower() in lowered for marker in markers)

    @staticmethod
    def _current_document(documents: list[SessionDocumentRecord]) -> SessionDocumentRecord | None:
        for document in documents:
            if document.is_current:
                return document
        return documents[0] if documents else None

    @staticmethod
    def _document_id(document: Any) -> str | None:
        document_id = str(getattr(document, "document_id", "") or "").strip()
        return document_id or None

    @staticmethod
    def _jsonable_signals(signals: dict[str, Any]) -> dict[str, Any]:
        return {
            key: sorted(value) if isinstance(value, set) else value
            for key, value in signals.items()
            if key not in {"pending_confirmations", "current_document"}
        }

    @staticmethod
    def bundle_to_metadata(bundle: NextActionBundle) -> dict[str, Any]:
        return json.loads(bundle.model_dump_json())
