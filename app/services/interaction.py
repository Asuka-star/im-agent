from dataclasses import dataclass


@dataclass(slots=True)
class InteractionDecision:
    mode: str
    label: str


class InteractionService:
    """Classifies whether a chat message is passive discussion or an explicit AI trigger."""

    SUMMARY_KEYWORDS = (
        "总结",
        "纪要",
        "梳理",
        "汇总",
        "回顾",
        "结论",
    )
    TASK_KEYWORDS = (
        "待办",
        "任务清单",
        "任务列表",
        "action items",
        "todo",
        "to-do",
    )
    RISK_KEYWORDS = (
        "风险",
        "阻塞",
        "卡点",
        "问题点",
    )
    STATUS_KEYWORDS = (
        "谁负责",
        "没负责人",
        "未分配",
        "截止时间",
        "到期",
        "进展",
        "状态",
        "还有哪些任务",
        "哪些任务",
    )
    SLIDE_KEYWORDS = (
        "演示稿",
        "汇报稿",
        "路演稿",
        "ppt",
        "PPT",
        "幻灯片",
        "大纲",
    )

    def decide(self, text: str) -> InteractionDecision:
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if self._contains_any(normalized, lowered, self.SLIDE_KEYWORDS):
            return InteractionDecision(mode="slides", label="生成演示稿大纲")
        if self._contains_any(normalized, lowered, self.STATUS_KEYWORDS):
            return InteractionDecision(mode="status", label="查询当前协作状态")
        if self._contains_any(normalized, lowered, self.RISK_KEYWORDS):
            return InteractionDecision(mode="risks", label="输出风险与卡点")
        if self._contains_any(normalized, lowered, self.TASK_KEYWORDS):
            return InteractionDecision(mode="tasks", label="整理任务清单")
        if self._contains_any(normalized, lowered, self.SUMMARY_KEYWORDS):
            return InteractionDecision(mode="summary", label="总结近期讨论")
        return InteractionDecision(mode="buffer", label="继续积累群聊讨论")

    def _contains_any(self, text: str, lowered: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text or keyword.lower() in lowered for keyword in keywords)
