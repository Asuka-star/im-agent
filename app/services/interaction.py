from dataclasses import dataclass

from app.services.request_router import RequestRouter


@dataclass(slots=True)
class InteractionDecision:
    mode: str
    label: str


class InteractionService:
    """Fallback intent classifier used only when the LLM path is unavailable."""

    ROUTE_LABELS = {
        "help": "展示可用能力",
        "doc": "整理讨论并同步文档",
        "slides": "生成演示稿大纲",
        "canvas": "生成自由画布",
        "status": "查询当前协作状态",
        "risks": "输出风险与卡点",
        "tasks": "整理任务清单",
        "summary": "总结近期讨论",
    }
    REQUEST_PREFIXES = ("帮我", "麻烦", "请", "可以", "能不能", "帮忙", "顺手")

    def __init__(self, *, router: RequestRouter | None = None) -> None:
        self.router = router or RequestRouter()

    def decide(self, text: str) -> InteractionDecision:
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if not normalized:
            return InteractionDecision(mode="help", label=self.ROUTE_LABELS["help"])

        route_decision = self.router.route_by_rule(normalized)
        if route_decision is not None:
            if route_decision.route in self.ROUTE_LABELS:
                return InteractionDecision(
                    mode=route_decision.route,
                    label=self.ROUTE_LABELS[route_decision.route],
                )
            if route_decision.needs_clarification:
                return InteractionDecision(mode="help", label=self.ROUTE_LABELS["help"])

        if self._looks_like_request(normalized, lowered):
            return InteractionDecision(mode="help", label=self.ROUTE_LABELS["help"])

        return InteractionDecision(mode="buffer", label="继续积累群聊讨论")

    def _looks_like_request(self, text: str, lowered: str) -> bool:
        return any(prefix in text or prefix.lower() in lowered for prefix in self.REQUEST_PREFIXES)
