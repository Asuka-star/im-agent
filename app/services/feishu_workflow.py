import logging

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.message_api import FeishuMessageAPI
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext


logger = logging.getLogger(__name__)


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
                logger.exception("Failed to send Feishu reply")

        return {
            "session_id": analysis.session_id,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
        }

    def _format_reply(self, analysis: AnalyzeResponse) -> str:
        lines = [
            "\u3010\u672c\u8f6e\u534f\u540c\u603b\u7ed3\u3011",
            f"1. \u6458\u8981\uff1a{analysis.summary}",
            "2. \u4efb\u52a1\uff1a",
        ]

        for idx, task in enumerate(analysis.tasks, start=1):
            lines.append(
                f"   {idx}) {task.title} | \u8d1f\u8d23\u4eba\uff1a{task.owner} | \u622a\u6b62\uff1a{task.due_date} | \u4f18\u5148\u7ea7\uff1a{task.priority}"
            )

        lines.append("3. \u98ce\u9669\uff1a")
        for idx, risk in enumerate(analysis.risks, start=1):
            lines.append(f"   {idx}) {risk}")

        lines.append("4. \u4e0b\u4e00\u6b65\u5efa\u8bae\uff1a")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"   {idx}) {action}")

        return "\n".join(lines)
