from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.message_api import FeishuMessageAPI
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext


class FeishuWorkflowService:
    """Bridges Feishu message events to the internal agent workflow."""

    def __init__(self) -> None:
        self.orchestrator = AgentOrchestrator()
        self.message_api = FeishuMessageAPI()

    def handle_message(self, message: FeishuMessageContext) -> dict:
        analysis = self.orchestrator.run(
            AnalyzeRequest(
                session_id=message.session_id,
                raw_text=message.text,
            )
        )

        reply_preview = self._format_reply(analysis)
        reply_sent = False
        reply_error: str | None = None

        if settings.feishu_reply_enabled and message.chat_id:
            try:
                self.message_api.send_text_message(
                    message.chat_id,
                    reply_preview,
                    receive_id_type="chat_id",
                )
                reply_sent = True
            except Exception as exc:  # noqa: BLE001
                reply_error = str(exc)

        return {
            "session_id": analysis.session_id,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
        }

    def _format_reply(self, analysis: AnalyzeResponse) -> str:
        lines = [
            "[Collaboration Summary]",
            f"1. Summary: {analysis.summary}",
            "2. Tasks:",
        ]

        for idx, task in enumerate(analysis.tasks, start=1):
            lines.append(
                f"   {idx}) {task.title} | Owner: {task.owner} | Due: {task.due_date} | Priority: {task.priority}"
            )

        lines.append("3. Risks:")
        for idx, risk in enumerate(analysis.risks, start=1):
            lines.append(f"   {idx}) {risk}")

        lines.append("4. Next actions:")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"   {idx}) {action}")

        return "\n".join(lines)
