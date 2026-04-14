from app.agents.orchestrator import AgentOrchestrator
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext


class FeishuWorkflowService:
    """Bridges Feishu message events to the internal agent workflow."""

    def __init__(self) -> None:
        self.orchestrator = AgentOrchestrator()

    def handle_message(self, message: FeishuMessageContext) -> dict:
        analysis = self.orchestrator.run(
            AnalyzeRequest(
                session_id=message.session_id,
                raw_text=message.text,
            )
        )

        return {
            "session_id": analysis.session_id,
            "analysis": analysis,
            "reply_preview": self._format_reply(analysis),
        }

    def _format_reply(self, analysis: AnalyzeResponse) -> str:
        lines = [
            "【本轮协同总结】",
            f"1. 摘要：{analysis.summary}",
            "2. 待办：",
        ]

        for idx, task in enumerate(analysis.tasks, start=1):
            lines.append(
                f"   {idx}) {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 优先级：{task.priority}"
            )

        lines.append("3. 风险：")
        for idx, risk in enumerate(analysis.risks, start=1):
            lines.append(f"   {idx}) {risk}")

        lines.append("4. 下一步建议：")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"   {idx}) {action}")

        return "\n".join(lines)
