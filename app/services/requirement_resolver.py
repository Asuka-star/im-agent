from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.schemas.requirement import RequirementResolveCandidate, RequirementResolveResult
from app.services.requirement_service import RequirementService


NEW_REQUIREMENT_MARKERS = (
    "新需求",
    "新建需求",
    "新建一个需求",
    "另一个需求",
    "另一个方案",
    "换个方向",
    "不是刚才",
    "重新做一个",
)

SKIP_COMMANDS = {
    "任务列表",
    "任务清单",
    "待办列表",
    "待办清单",
    "当前任务",
    "当前待办",
    "查看任务",
    "查询任务",
    "风险列表",
    "风险清单",
    "帮助",
    "使用说明",
    "help",
}

LIFECYCLE_MARKERS = (
    "需求",
    "方案",
    "文档",
    "doc",
    "ppt",
    "演示稿",
    "画布",
    "canvas",
    "流程图",
    "汇报",
    "产品",
    "系统",
    "报名",
    "审核",
    "交付",
    "继续",
    "修改",
    "基于",
    "上一版",
    "刚才",
)


@dataclass(frozen=True)
class RequirementResolveInput:
    session_id: str
    text: str
    sender_id: str | None = None
    message_id: str | None = None
    source_type: str = "im"


class RequirementResolver:
    """Resolves incoming work to a requirement workspace with LLM-first semantics."""

    def __init__(self, requirement_service: RequirementService | None = None, llm_service: Any | None = None) -> None:
        self.requirement_service = requirement_service or RequirementService()
        self.llm_service = llm_service

    def resolve(self, payload: RequirementResolveInput | Any) -> RequirementResolveResult:
        session_id = str(getattr(payload, "session_id", "") or "").strip()
        text = str(getattr(payload, "text", "") or "").strip()
        sender_id = str(getattr(payload, "sender_id", "") or "").strip() or None
        message_id = str(getattr(payload, "message_id", "") or "").strip() or None
        source_type = str(getattr(payload, "source_type", "") or "im").strip() or "im"
        passive = source_type.startswith("im_passive")
        if not session_id or not text:
            return RequirementResolveResult(action="skip", reason="缺少会话或文本，跳过需求归属。")

        try:
            active_requirements = self.requirement_service.list_requirements(status="active", limit=50)
            related_requirements = self.requirement_service.list_requirements(
                session_id=session_id,
                status="active",
                limit=50,
            )
        except Exception:
            return RequirementResolveResult(action="skip", reason="需求工作区存储暂不可用，跳过需求归属。")
        active_requirements = _merge_requirements(active_requirements, related_requirements)
        related_requirement_ids = {item.requirement_id for item in related_requirements}
        session_requirements = [
            item
            for item in active_requirements
            if item.primary_session_id == session_id or item.requirement_id in related_requirement_ids
        ]
        new_requested = _contains_any(text, NEW_REQUIREMENT_MARKERS)
        lifecycle_like = _is_lifecycle_like(text)

        if _compact(text) in {_compact(item) for item in SKIP_COMMANDS} and not session_requirements:
            return RequirementResolveResult(action="skip", reason="这是纯查询/帮助类短命令，且当前会话没有活跃需求。")

        llm_result = self._resolve_with_llm(
            session_id=session_id,
            text=text,
            sender_id=sender_id,
            message_id=message_id,
            active_requirements=active_requirements,
            session_requirements=session_requirements,
            related_requirement_ids=related_requirement_ids,
            new_requested=new_requested,
            lifecycle_like=lifecycle_like,
            source_type=source_type,
            passive=passive,
        )
        if llm_result is not None:
            return llm_result

        if passive:
            return RequirementResolveResult(action="skip", reason="未 @ 机器人的群聊消息只做 LLM 被动归属；LLM 不可用或不确定时不创建需求。")

        return self._resolve_by_rules(
            session_id=session_id,
            text=text,
            sender_id=sender_id,
            message_id=message_id,
            source_type=source_type,
            active_requirements=active_requirements,
            session_requirements=session_requirements,
            new_requested=new_requested,
            lifecycle_like=lifecycle_like,
        )

    def _resolve_by_rules(
        self,
        *,
        session_id: str,
        text: str,
        sender_id: str | None,
        message_id: str | None,
        source_type: str,
        active_requirements: list,
        session_requirements: list,
        new_requested: bool,
        lifecycle_like: bool,
    ) -> RequirementResolveResult:
        if new_requested or (lifecycle_like and not active_requirements):
            created = self.requirement_service.create_requirement(
                title=_title_from_text(text),
                primary_session_id=session_id,
                summary=_summary_from_text(text),
                created_by=sender_id,
                source_message_id=message_id,
                source_type=source_type,
                metadata={
                    "requirement_binding": {
                        "source": "resolver_create",
                        "reason": "explicit_new" if new_requested else "no_active_requirement",
                    }
                },
            )
            return RequirementResolveResult(
                action="create",
                requirement_id=created.requirement_id,
                confidence=0.95 if new_requested else 0.78,
                matched_by="new_request" if new_requested else "no_active_requirement",
                reason="用户明确开启新需求。" if new_requested else "当前没有活跃需求，已为需求类请求创建新需求。",
            )

        candidates = self._score_candidates(text, active_requirements, session_id=session_id)
        if not candidates and lifecycle_like and not session_requirements:
            created = self.requirement_service.create_requirement(
                title=_title_from_text(text),
                primary_session_id=session_id,
                summary=_summary_from_text(text),
                created_by=sender_id,
                source_message_id=message_id,
                source_type=source_type,
                metadata={
                    "requirement_binding": {
                        "source": "resolver_create",
                        "reason": "no_session_requirement",
                    }
                },
            )
            return RequirementResolveResult(
                action="create",
                requirement_id=created.requirement_id,
                confidence=0.76,
                matched_by="no_session_requirement",
                reason="当前会话没有活跃需求，已为需求类请求创建新需求。",
            )
        if not candidates:
            return RequirementResolveResult(action="skip", reason="没有可匹配的活跃需求，且当前请求不像需求生命周期请求。")

        best = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None
        if best.score >= 0.82 and (second is None or best.score - second.score >= 0.16):
            return RequirementResolveResult(
                action="bind",
                requirement_id=best.requirement_id,
                confidence=min(best.score, 0.99),
                matched_by="explicit_title",
                reason=best.reason or "命中了已有需求标题或摘要。",
                candidates=[best],
            )

        if len(session_requirements) == 1 and lifecycle_like:
            only = session_requirements[0]
            return RequirementResolveResult(
                action="bind",
                requirement_id=only.requirement_id,
                confidence=0.72,
                matched_by="single_active",
                reason="当前会话只有一个活跃需求，且请求属于需求生命周期动作。",
                candidates=[
                    RequirementResolveCandidate(
                        requirement_id=only.requirement_id,
                        title=only.title,
                        summary=only.summary,
                        score=0.72,
                        reason="当前会话唯一活跃需求。",
                    )
                ],
            )

        if lifecycle_like or candidates[0].score >= 0.45:
            return RequirementResolveResult(
                action="clarify",
                confidence=candidates[0].score,
                matched_by="multiple_candidates",
                reason="本次请求可能属于多个需求，需要用户确认归属。",
                candidates=candidates[:5],
            )

        return RequirementResolveResult(action="skip", reason="请求不需要绑定需求工作区。")

    def _resolve_with_llm(
        self,
        *,
        session_id: str,
        text: str,
        sender_id: str | None,
        message_id: str | None,
        active_requirements: list,
        session_requirements: list,
        related_requirement_ids: set[str],
        new_requested: bool,
        lifecycle_like: bool,
        source_type: str,
        passive: bool,
    ) -> RequirementResolveResult | None:
        if self.llm_service is None or not getattr(self.llm_service, "is_configured", lambda: False)():
            return None
        try:
            raw = self.llm_service.resolve_requirement_workspace(
                {
                    "session_id": session_id,
                    "sender_id": sender_id,
                    "message_id": message_id,
                    "text": text,
                    "source_type": source_type,
                    "signals": {
                        "explicit_new_request": new_requested,
                        "lifecycle_like_by_rules": lifecycle_like,
                        "session_active_requirement_count": len(session_requirements),
                        "passive_group_observation": passive,
                    },
                    "active_requirements": [
                        {
                            "requirement_id": item.requirement_id,
                            "title": item.title,
                            "status": item.status,
                            "summary": item.summary,
                            "primary_session_id": item.primary_session_id,
                            "current_document_id": item.current_document_id,
                            "current_slides_artifact_id": item.current_slides_artifact_id,
                            "current_canvas_artifact_id": item.current_canvas_artifact_id,
                            "current_delivery_artifact_id": item.current_delivery_artifact_id,
                            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                            "related_to_current_session": (
                                item.primary_session_id == session_id
                                or item.requirement_id in related_requirement_ids
                            ),
                        }
                        for item in active_requirements[:50]
                    ],
                }
            )
        except Exception:
            return None
        return self._result_from_llm(
            raw,
            session_id=session_id,
            text=text,
            sender_id=sender_id,
            message_id=message_id,
            active_requirements=active_requirements,
            lifecycle_like=lifecycle_like,
            new_requested=new_requested,
            source_type=source_type,
            passive=passive,
        )

    def _result_from_llm(
        self,
        raw: dict,
        *,
        session_id: str,
        text: str,
        sender_id: str | None,
        message_id: str | None,
        active_requirements: list,
        lifecycle_like: bool,
        new_requested: bool,
        source_type: str,
        passive: bool,
    ) -> RequirementResolveResult | None:
        if not isinstance(raw, dict):
            return None
        action = str(raw.get("action") or "").strip().lower()
        confidence = _float_between(raw.get("confidence"), default=0.0)
        reason = str(raw.get("reason") or "").strip() or "LLM 完成需求归属判断。"
        matched_by = str(raw.get("matched_by") or "llm").strip() or "llm"
        by_id = {item.requirement_id: item for item in active_requirements}
        candidates = self._llm_candidates(raw.get("candidates"), by_id)
        requirement_id = str(raw.get("requirement_id") or "").strip()

        if action == "bind":
            bind_threshold = 0.74 if passive else 0.70
            if requirement_id in by_id and confidence >= bind_threshold:
                candidate = self._candidate_from_requirement(
                    by_id[requirement_id],
                    score=confidence,
                    reason=reason,
                )
                return RequirementResolveResult(
                    action="bind",
                    requirement_id=requirement_id,
                    confidence=confidence,
                    matched_by=matched_by,
                    reason=reason,
                    candidates=[candidate],
                )
            if candidates:
                fallback_candidates = self._merge_with_recent_candidates(
                    candidates,
                    text,
                    active_requirements,
                    session_id=session_id,
                )
                return RequirementResolveResult(
                    action="clarify",
                    confidence=confidence,
                    matched_by="llm_low_confidence",
                    reason="LLM 给出了候选，但置信度不足或目标需求无效，需要用户确认。",
                    candidates=fallback_candidates[:5],
                )
            fallback_candidates = self._fallback_clarification_candidates(text, active_requirements, session_id=session_id)
            return RequirementResolveResult(
                action="clarify",
                confidence=confidence,
                matched_by="llm_low_confidence",
                reason="LLM 倾向绑定已有需求，但置信度不足，需要用户确认归属。",
                candidates=fallback_candidates[:5],
            )

        if action == "create":
            create_threshold = 0.78 if passive else 0.62
            if confidence < create_threshold and not (new_requested and not passive):
                fallback_candidates = self._merge_with_recent_candidates(
                    candidates,
                    text,
                    active_requirements,
                    session_id=session_id,
                )
                return RequirementResolveResult(
                    action="clarify",
                    confidence=confidence,
                    matched_by="llm_low_confidence_create",
                    reason="LLM 倾向新建需求但置信度不足，需要用户确认。",
                    candidates=fallback_candidates[:5],
                )
            new_payload = raw.get("new_requirement") if isinstance(raw.get("new_requirement"), dict) else {}
            created = self.requirement_service.create_requirement(
                title=str(new_payload.get("title") or "").strip() or _title_from_text(text),
                primary_session_id=session_id,
                summary=str(new_payload.get("summary") or "").strip() or _summary_from_text(text),
                created_by=sender_id,
                source_message_id=message_id,
                source_type=source_type,
                metadata={
                    "requirement_binding": {
                        "source": "llm_resolver_create",
                        "confidence": confidence,
                        "reason": reason,
                    }
                },
            )
            return RequirementResolveResult(
                action="create",
                requirement_id=created.requirement_id,
                confidence=confidence,
                matched_by=matched_by or "llm_create",
                reason=reason,
            )

        if action == "clarify":
            fallback_candidates = self._merge_with_recent_candidates(
                candidates,
                text,
                active_requirements,
                session_id=session_id,
            )
            return RequirementResolveResult(
                action="clarify",
                confidence=confidence,
                matched_by=matched_by or "llm_clarify",
                reason=reason,
                candidates=fallback_candidates[:5],
            )

        if action == "skip":
            if lifecycle_like and confidence < 0.72:
                return None
            return RequirementResolveResult(
                action="skip",
                confidence=confidence,
                matched_by=matched_by or "llm_skip",
                reason=reason,
                candidates=candidates[:5],
            )

        return None

    def _merge_with_recent_candidates(
        self,
        candidates: list[RequirementResolveCandidate],
        text: str,
        requirements: list,
        *,
        session_id: str,
    ) -> list[RequirementResolveCandidate]:
        merged = list(candidates)
        seen = {item.requirement_id for item in merged}
        for item in self._fallback_clarification_candidates(text, requirements, session_id=session_id):
            if item.requirement_id in seen:
                continue
            seen.add(item.requirement_id)
            merged.append(item)
            if len(merged) >= 5:
                break
        return merged

    def _llm_candidates(self, raw_candidates: Any, by_id: dict[str, Any]) -> list[RequirementResolveCandidate]:
        if not isinstance(raw_candidates, list):
            return []
        candidates: list[RequirementResolveCandidate] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            requirement_id = str(raw.get("requirement_id") or "").strip()
            if requirement_id not in by_id or requirement_id in seen:
                continue
            seen.add(requirement_id)
            candidates.append(
                self._candidate_from_requirement(
                    by_id[requirement_id],
                    score=_float_between(raw.get("score"), default=0.0),
                    reason=str(raw.get("reason") or "").strip() or "LLM 候选需求。",
                )
            )
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    @staticmethod
    def _candidate_from_requirement(item: Any, *, score: float, reason: str) -> RequirementResolveCandidate:
        return RequirementResolveCandidate(
            requirement_id=item.requirement_id,
            title=item.title,
            summary=item.summary,
            score=round(score, 2),
            reason=reason,
        )

    def _fallback_clarification_candidates(
        self,
        text: str,
        requirements: list,
        *,
        session_id: str,
    ) -> list[RequirementResolveCandidate]:
        scored = self._score_candidates(text, requirements, session_id=session_id)
        session_first = sorted(
            requirements,
            key=lambda item: (
                item.primary_session_id != session_id,
                -_timestamp(getattr(item, "updated_at", None)),
                getattr(item, "title", ""),
            ),
        )
        merged = list(scored)
        seen = {item.requirement_id for item in merged}
        for item in session_first:
            if item.requirement_id in seen:
                continue
            seen.add(item.requirement_id)
            merged.append(
                self._candidate_from_requirement(
                    item,
                    score=0.0,
                    reason="最近活跃需求，等待用户确认。",
                )
            )
            if len(merged) >= 5:
                break
        return merged

    def _score_candidates(
        self,
        text: str,
        requirements: list,
        *,
        session_id: str,
    ) -> list[RequirementResolveCandidate]:
        compact_text = _compact(text)
        tokens = _tokens(text)
        scored: list[RequirementResolveCandidate] = []
        for item in requirements:
            compact_title = _compact(item.title)
            compact_summary = _compact(item.summary)
            title_tokens = _tokens(item.title)
            summary_tokens = _tokens(item.summary or "")
            score = 0.0
            reasons: list[str] = []
            if compact_title and compact_title in compact_text:
                score += 0.9
                reasons.append("命中需求标题")
            elif compact_text and compact_text in compact_title and len(compact_text) >= 4:
                score += 0.65
                reasons.append("用户输入命中需求标题片段")
            shared_title_tokens = tokens & title_tokens
            if shared_title_tokens:
                score += min(0.45, len(shared_title_tokens) * 0.15)
                reasons.append("与需求标题存在关键词重合")
            shared_summary_tokens = tokens & summary_tokens
            if shared_summary_tokens:
                score += min(0.25, len(shared_summary_tokens) * 0.08)
                reasons.append("与需求摘要存在关键词重合")
            if item.primary_session_id == session_id:
                score += 0.18
                reasons.append("来自当前会话")
            if score <= 0:
                continue
            scored.append(
                RequirementResolveCandidate(
                    requirement_id=item.requirement_id,
                    title=item.title,
                    summary=item.summary,
                    score=round(min(score, 0.99), 2),
                    reason="，".join(reasons),
                )
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)


def _title_from_text(text: str) -> str:
    cleaned = re.sub(r"^(@\S+\s*)+", "", str(text or "")).strip()
    cleaned = re.sub(r"(帮我|请|麻烦|生成|整理|撰写|写一份|做一个|新建一个|新建)", "", cleaned)
    cleaned = re.sub(r"(需求文档|需求方案|方案文档|演示稿|PPT|ppt|画布|流程图|交付包)", "", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ，。:：")
    if not cleaned:
        return "未命名需求"
    return cleaned[:32]


def _summary_from_text(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    return cleaned[:220]


def _is_lifecycle_like(text: str) -> bool:
    compact = _compact(text)
    if compact in {_compact(item) for item in SKIP_COMMANDS}:
        return False
    return _contains_any(text, LIFECYCLE_MARKERS)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.lower() in str(text or "").lower() for marker in markers)


def _compact(value: str | None) -> str:
    return "".join(str(value or "").lower().split())


def _merge_requirements(primary: list, secondary: list) -> list:
    merged = list(primary)
    seen = {getattr(item, "requirement_id", None) for item in merged}
    for item in secondary:
        requirement_id = getattr(item, "requirement_id", None)
        if requirement_id in seen:
            continue
        seen.add(requirement_id)
        merged.append(item)
    return merged


def _float_between(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


def _timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except (TypeError, ValueError, OSError):
            return 0.0
    return 0.0


def _tokens(value: str | None) -> set[str]:
    text = str(value or "").lower()
    ascii_tokens = {item for item in re.findall(r"[a-z0-9_]{2,}", text) if item}
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    chinese_tokens: set[str] = set()
    for chunk in chinese_chunks:
        if len(chunk) <= 4:
            chinese_tokens.add(chunk)
            continue
        for size in (2, 3, 4):
            for index in range(0, len(chunk) - size + 1):
                chinese_tokens.add(chunk[index : index + size])
    stop_tokens = {"这个", "那个", "刚才", "需求", "方案", "文档", "生成", "修改", "继续", "一下"}
    return {token for token in ascii_tokens | chinese_tokens if token not in stop_tokens}
