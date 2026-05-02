from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.artifact_edit_plan import ArtifactEditPlanner


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    source: str
    confidence: float
    needs_clarification: bool = False
    reason: str = ""
    requested_outputs: tuple[str, ...] = ()
    defer_to_llm: bool = False


class RequestRouter:
    """Routes high-frequency collaboration requests before deep LLM planning."""

    ROUTES = {"status", "summary", "tasks", "risks", "doc", "slides", "canvas", "help", "unknown"}

    HELP_KEYWORDS = ("怎么用", "你能做什么", "能做什么", "help", "帮助", "使用说明")
    DOC_KEYWORDS = (
        "文档",
        "飞书文档",
        "协作文档",
        "需求文档",
        "写成材料",
        "整理成材料",
        "沉淀成材料",
        "同步到文档",
        "写入文档",
        "写到文档",
    )
    DOC_ACTION_KEYWORDS = ("写成", "整理成", "总结成", "沉淀", "同步", "写入", "写到", "生成", "更新")
    SLIDES_KEYWORDS = (
        "ppt",
        "slides",
        "presentation",
        "演示稿",
        "演示提纲",
        "幻灯片",
        "汇报ppt",
        "汇报大纲",
        "汇报提纲",
        "汇报材料",
        "报告大纲",
        "路演稿",
        "演讲稿",
    )
    CANVAS_KEYWORDS = (
        "canvas",
        "whiteboard",
        "board",
        "flowchart",
        "diagram",
        "architecture diagram",
        "mind map",
        "白板",
        "画布",
        "流程图",
        "架构图",
        "思维导图",
        "画一张图",
    )
    STATUS_KEYWORDS = (
        "进度",
        "状态",
        "到哪",
        "谁负责",
        "负责人",
        "截止时间",
        "什么时候完成",
        "当前还有哪些",
        "还有哪些任务",
        "有哪些任务",
        "查看任务",
        "查询任务",
        "任务列表",
        "待办列表",
        "待办清单",
    )
    TASK_ANALYSIS_KEYWORDS = ("整理任务", "提取任务", "提取待办", "生成任务", "更新任务", "同步任务")
    SUMMARY_KEYWORDS = ("总结", "纪要", "梳理", "回顾", "归纳", "结论")
    RISK_KEYWORDS = ("风险", "阻塞", "卡点", "问题点", "风险项")
    ARTIFACT_OUTPUT_ACTIONS = (
        "生成",
        "写成",
        "写到",
        "写入",
        "同步到",
        "沉淀到",
        "沉淀成",
        "整理成",
        "总结成",
        "输出到",
        "导出到",
        "做成",
        "转成",
        "形成",
        "产出",
        "make",
        "create",
        "generate",
        "write",
        "sync",
        "export",
        "turn into",
        "convert to",
    )
    ARTIFACT_LOCAL_TARGET_MARKERS = (
        "里面",
        "里",
        "中的",
        "中",
        "以下",
        "以上",
        "后面",
        "前面",
        "之后",
        "之前",
        "栏",
        "栏目",
        "章节",
        "部分",
        "页面",
        "节点",
        "第",
        "section",
        "page",
        "slide",
        "node",
    )
    VAGUE_REQUESTS = (
        "帮我整理",
        "整理一下",
        "处理一下",
        "怎么处理",
        "怎么办",
        "弄一下",
        "弄一版",
        "看一下",
        "你看着办",
    )
    LOW_CONFIDENCE_THRESHOLD = 0.45

    def route(self, instruction: str, *, llm_service: Any | None = None) -> RouteDecision:
        rule_decision = self.route_by_rule(instruction)
        if rule_decision is not None and (not rule_decision.needs_clarification or not rule_decision.defer_to_llm):
            return rule_decision

        if llm_service is not None and getattr(llm_service, "is_configured", lambda: False)():
            try:
                result = llm_service.route_workspace_request(instruction)
                return self.route_from_llm_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lightweight request routing failed, using fallback route: %s", exc)

        if rule_decision is not None:
            return rule_decision

        if self._is_vague_request(instruction):
            return RouteDecision(
                route="unknown",
                source="fallback",
                confidence=0.2,
                needs_clarification=True,
                reason="请求目标不明确，需要先确认要输出的产物。",
            )
        return RouteDecision(route="unknown", source="fallback", confidence=0.0, reason="未命中规则路由。")

    def route_by_rule(self, instruction: str) -> RouteDecision | None:
        text = (instruction or "").strip()
        lowered = text.lower()
        if not text:
            return RouteDecision(route="help", source="rule", confidence=1.0, reason="空请求，展示帮助。")

        if self._contains_any(text, lowered, self.HELP_KEYWORDS):
            return RouteDecision(route="help", source="rule", confidence=1.0, reason="用户询问使用方式。")

        doc_requested = self._is_doc_request(text, lowered)
        slides_requested = self._is_slides_request(text, lowered)
        canvas_requested = self._is_canvas_request(text, lowered)
        output_requested = self._is_artifact_output_request(
            text,
            lowered,
            doc_requested=doc_requested,
            slides_requested=slides_requested,
            canvas_requested=canvas_requested,
        )

        if (
            (doc_requested or slides_requested or canvas_requested)
            and not output_requested
            and self._needs_artifact_edit_clarification(text, lowered)
        ):
            return RouteDecision(
                route="unknown",
                source="rule",
                confidence=0.42,
                needs_clarification=True,
                reason="用户指出了产物和局部范围，但没有明确要删除、移动、改写还是补充。",
                defer_to_llm=True,
            )

        requested_outputs = self._requested_outputs(
            text=text,
            lowered=lowered,
            doc_requested=doc_requested,
            slides_requested=slides_requested,
            canvas_requested=canvas_requested,
        )
        primary_artifact = requested_outputs[0] if requested_outputs else ""

        if primary_artifact == "doc":
            reason = (
                "用户要求生成文档并补充其他协作产物。"
                if len(requested_outputs) > 1
                else "用户明确要求生成或更新文档。"
            )
            return RouteDecision(
                route="doc",
                source="rule",
                confidence=0.98,
                reason=reason,
                requested_outputs=requested_outputs,
            )

        if primary_artifact == "slides":
            reason = (
                "用户要求生成演示稿并补充画布产物。"
                if canvas_requested
                else "用户明确要求生成演示稿或汇报材料。"
            )
            return RouteDecision(
                route="slides",
                source="rule",
                confidence=0.96,
                reason=reason,
                requested_outputs=requested_outputs,
            )

        if primary_artifact == "canvas":
            return RouteDecision(
                route="canvas",
                source="rule",
                confidence=0.94,
                reason="Request asks for a canvas or diagram artifact.",
                requested_outputs=requested_outputs,
            )

        if self._is_status_request(text, lowered):
            return RouteDecision(route="status", source="rule", confidence=0.95, reason="用户在查询当前协作状态。")

        if self._contains_any(text, lowered, self.RISK_KEYWORDS):
            return RouteDecision(route="risks", source="rule", confidence=0.9, reason="用户要求识别风险或卡点。")

        if self._contains_any(text, lowered, self.TASK_ANALYSIS_KEYWORDS):
            return RouteDecision(route="tasks", source="rule", confidence=0.9, reason="用户要求整理或更新任务。")

        if self._contains_any(text, lowered, self.SUMMARY_KEYWORDS):
            return RouteDecision(route="summary", source="rule", confidence=0.82, reason="用户要求总结讨论。")

        if self._is_vague_request(text):
            return RouteDecision(
                route="unknown",
                source="rule",
                confidence=0.4,
                needs_clarification=True,
                reason="请求较模糊，无法确定要总结、写文档还是生成演示稿。",
            )

        return None

    def route_from_llm_result(self, result: dict[str, Any]) -> RouteDecision:
        route = self._normalize_route(result.get("route") or result.get("intent"))
        confidence = self._normalize_confidence(result.get("confidence"))
        if route not in self.ROUTES:
            route = "unknown"
        needs_clarification = bool(result.get("needs_clarification"))
        if route == "unknown" or confidence < self.LOW_CONFIDENCE_THRESHOLD:
            needs_clarification = True
        return RouteDecision(
            route=route,
            source="llm",
            confidence=confidence,
            needs_clarification=needs_clarification,
            reason=str(result.get("reason") or "").strip(),
            requested_outputs=self._normalize_requested_outputs(result.get("requested_outputs"), fallback_route=route),
        )

    def _is_doc_request(self, text: str, lowered: str) -> bool:
        if self._is_doc_excluded(text, lowered):
            return False
        if self._contains_any(text, lowered, self.DOC_KEYWORDS):
            return True
        return ("文档" in text or "doc" in lowered or "document" in lowered) and self._contains_any(
            text,
            lowered,
            self.DOC_ACTION_KEYWORDS,
        )

    def _is_slides_request(self, text: str, lowered: str) -> bool:
        if self._is_slides_excluded(text, lowered):
            return False
        if self._contains_any(text, lowered, self.SLIDES_KEYWORDS):
            return True
        return "汇报材料" in text and "文档" not in text

    def _is_canvas_request(self, text: str, lowered: str) -> bool:
        return self._contains_any(text, lowered, self.CANVAS_KEYWORDS)

    def _is_doc_excluded(self, text: str, lowered: str) -> bool:
        return self._contains_any(
            text,
            lowered,
            (
                "不要写文档",
                "不用写文档",
                "别写文档",
                "不写文档",
                "不生成文档",
                "不要文档",
                "不用文档",
                "只生成ppt",
                "只做ppt",
                "只要ppt",
                "only slides",
                "slides only",
            ),
        )

    def _is_slides_excluded(self, text: str, lowered: str) -> bool:
        return self._contains_any(
            text,
            lowered,
            (
                "不要ppt",
                "不用ppt",
                "不生成ppt",
                "不要生成ppt",
                "不要演示稿",
                "不用演示稿",
                "不生成演示稿",
                "不要汇报材料",
                "不用汇报材料",
            ),
        )

    def _is_status_request(self, text: str, lowered: str) -> bool:
        if self._contains_any(text, lowered, self.STATUS_KEYWORDS):
            return not self._contains_any(text, lowered, self.TASK_ANALYSIS_KEYWORDS)
        return False

    def _is_artifact_output_request(
        self,
        text: str,
        lowered: str,
        *,
        doc_requested: bool,
        slides_requested: bool,
        canvas_requested: bool,
    ) -> bool:
        if not (doc_requested or slides_requested or canvas_requested):
            return False
        return self._contains_any(text, lowered, self.ARTIFACT_OUTPUT_ACTIONS)

    def _needs_artifact_edit_clarification(self, text: str, lowered: str) -> bool:
        action_text = re.sub(r"(?<![A-Za-z])update\s*\([^)]*\)", "", text, flags=re.IGNORECASE)
        action_lowered = action_text.lower()
        if any(marker in action_text or marker in action_lowered for marker in ArtifactEditPlanner.MUTATION_MARKERS):
            return False
        return self._contains_any(text, lowered, self.ARTIFACT_LOCAL_TARGET_MARKERS)

    def _is_vague_request(self, instruction: str) -> bool:
        text = (instruction or "").strip()
        lowered = text.lower()
        return self._contains_any(text, lowered, self.VAGUE_REQUESTS)

    @staticmethod
    def _requested_outputs(
        *,
        text: str = "",
        lowered: str = "",
        doc_requested: bool,
        slides_requested: bool,
        canvas_requested: bool,
    ) -> tuple[str, ...]:
        candidates: list[tuple[int, int, str]] = []
        if doc_requested:
            candidates.append((RequestRouter._first_keyword_index(text, lowered, RequestRouter.DOC_KEYWORDS), 0, "doc"))
        if slides_requested:
            candidates.append((RequestRouter._first_keyword_index(text, lowered, RequestRouter.SLIDES_KEYWORDS), 1, "slides"))
        if canvas_requested:
            candidates.append((RequestRouter._first_keyword_index(text, lowered, RequestRouter.CANVAS_KEYWORDS), 2, "canvas"))

        outputs: list[str] = []
        for _, _, output in sorted(candidates, key=lambda item: item[:2]):
            if output not in outputs:
                outputs.append(output)
        return tuple(outputs)

    @staticmethod
    def _first_keyword_index(text: str, lowered: str, keywords: tuple[str, ...]) -> int:
        positions: list[int] = []
        for keyword in keywords:
            raw_keyword = str(keyword or "")
            if not raw_keyword:
                continue
            candidates = [
                text.find(raw_keyword),
                lowered.find(raw_keyword.lower()),
            ]
            positions.extend(position for position in candidates if position >= 0)
        return min(positions) if positions else 10**9

    def _contains_any(self, text: str, lowered: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text or keyword.lower() in lowered for keyword in keywords)

    def _normalize_route(self, value: object) -> str:
        route = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "task": "tasks",
            "todo": "tasks",
            "todos": "tasks",
            "risk": "risks",
            "document": "doc",
            "feishu_doc": "doc",
            "ppt": "slides",
            "presentation": "slides",
            "whiteboard": "canvas",
            "board": "canvas",
            "diagram": "canvas",
            "flowchart": "canvas",
        }
        return aliases.get(route, route)

    def _normalize_confidence(self, value: object) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(confidence, 1.0))

    def _normalize_requested_outputs(self, value: object, *, fallback_route: str) -> tuple[str, ...]:
        raw_items = value if isinstance(value, list) else []
        outputs: list[str] = []
        for item in raw_items:
            route = self._normalize_route(item)
            if route in {"doc", "slides", "canvas"} and route not in outputs:
                outputs.append(route)
        if not outputs and fallback_route in {"doc", "slides", "canvas"}:
            outputs.append(fallback_route)
        return tuple(outputs)
