from dataclasses import dataclass


@dataclass(slots=True)
class InteractionDecision:
    mode: str
    label: str


class InteractionService:
    """Fallback intent classifier used only when the LLM path is unavailable."""

    SUMMARY_KEYWORDS = ("总结", "纪要", "梳理", "回顾", "结论", "归纳")
    TASK_KEYWORDS = ("待办", "任务清单", "任务列表", "action items", "todo", "to-do")
    RISK_KEYWORDS = ("风险", "阻塞", "卡点", "问题点", "风险项")
    STATUS_KEYWORDS = (
        "查看任务",
        "查询任务",
        "任务列表",
        "任务清单",
        "待办列表",
        "待办清单",
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
    SLIDE_KEYWORDS = ("演示稿", "汇报稿", "路演稿", "演讲稿", "ppt", "PPT", "幻灯片", "大纲")
    DOC_KEYWORDS = ("文档", "飞书文档", "文稿", "稿子")
    SYNC_KEYWORDS = ("同步", "写入", "写到", "更新", "记录", "存到", "放到", "放进", "落到")
    HELP_KEYWORDS = ("怎么用", "你能做什么", "能做什么", "help", "帮助")
    REQUEST_PREFIXES = ("帮我", "麻烦", "请", "可以", "能不能", "帮忙", "顺手")

    def decide(self, text: str) -> InteractionDecision:
        normalized = (text or "").strip()
        lowered = normalized.lower()

        if not normalized:
            return InteractionDecision(mode="help", label="展示可用能力")

        if self._contains_any(normalized, lowered, self.HELP_KEYWORDS):
            return InteractionDecision(mode="help", label="展示可用能力")

        if self._is_doc_request(normalized, lowered):
            return InteractionDecision(mode="doc", label="整理讨论并同步文档")
        if self._is_slide_request(normalized, lowered):
            return InteractionDecision(mode="slides", label="生成演示稿大纲")
        if self._contains_any(normalized, lowered, self.STATUS_KEYWORDS):
            return InteractionDecision(mode="status", label="查询当前协作状态")
        if self._contains_any(normalized, lowered, self.RISK_KEYWORDS):
            return InteractionDecision(mode="risks", label="输出风险与卡点")
        if self._contains_any(normalized, lowered, self.TASK_KEYWORDS):
            return InteractionDecision(mode="tasks", label="整理任务清单")
        if self._contains_any(normalized, lowered, self.SUMMARY_KEYWORDS):
            return InteractionDecision(mode="summary", label="总结近期讨论")

        if self._looks_like_request(normalized, lowered):
            return InteractionDecision(mode="help", label="展示可用能力")

        return InteractionDecision(mode="buffer", label="继续积累群聊讨论")

    def _is_doc_request(self, text: str, lowered: str) -> bool:
        mentions_doc = self._contains_any(text, lowered, self.DOC_KEYWORDS)
        mentions_sync = self._contains_any(text, lowered, self.SYNC_KEYWORDS)
        mentions_summary = self._contains_any(text, lowered, self.SUMMARY_KEYWORDS) or "整理" in text
        mentions_report = "汇报" in text or "路演" in text or "演示" in text or "大纲" in text
        return mentions_doc and (mentions_sync or mentions_summary or mentions_report)

    def _is_slide_request(self, text: str, lowered: str) -> bool:
        if self._contains_any(text, lowered, self.SLIDE_KEYWORDS):
            return True
        return self._contains_any(text, lowered, self.DOC_KEYWORDS) and (
            "汇报" in text or "路演" in text or "演示" in text
        )

    def _looks_like_request(self, text: str, lowered: str) -> bool:
        return self._contains_any(text, lowered, self.REQUEST_PREFIXES)

    def _contains_any(self, text: str, lowered: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text or keyword.lower() in lowered for keyword in keywords)
