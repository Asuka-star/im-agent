import logging

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.message_api import FeishuMessageAPI
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.services.interaction import InteractionDecision, InteractionService
from app.services.llm import LLMService
from app.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


class FeishuWorkflowService:
    """Handles buffered collaboration, explicit AI triggers, and reply delivery."""

    def __init__(self) -> None:
        self.orchestrator = AgentOrchestrator()
        self.message_api = FeishuMessageAPI()
        self.memory_service = MemoryService()
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()

    def handle_message(self, message: FeishuMessageContext) -> dict:
        self.memory_service.save_user_message(
            session_id=message.session_id,
            message_id=message.message_id,
            sender_id=message.sender_id,
            content=message.text or message.raw_text,
        )
        if message.chat_type == "group" and not message.is_mentioned:
            decision = InteractionDecision(mode="buffer", label="群聊普通讨论，继续旁听")
        else:
            decision = self.interaction_service.decide(message.text)

        if decision.mode == "buffer":
            return {
                "session_id": message.session_id,
                "mode": decision.mode,
                "reply_preview": None,
                "reply_sent": False,
                "reply_error": None,
                "analysis": None,
            }

        if decision.mode in {"summary", "tasks", "risks"}:
            result = self._handle_analysis_trigger(message, decision)
        elif decision.mode == "status":
            result = self._handle_status_trigger(message, decision)
        elif decision.mode == "slides":
            result = self._handle_slides_trigger(message, decision)
        else:
            result = {
                "session_id": message.session_id,
                "mode": "buffer",
                "reply_preview": None,
                "reply_sent": False,
                "reply_error": None,
                "analysis": None,
            }

        if result["reply_preview"]:
            self.memory_service.save_assistant_message(
                session_id=message.session_id,
                content=result["reply_preview"],
            )

        return result

    def _handle_analysis_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict:
        discussion_block = self.memory_service.build_discussion_block(
            message.session_id,
            exclude_message_id=message.message_id,
        )
        if not discussion_block:
            reply = (
                "我已经开始旁听这段群聊了，但这轮还没有积累到可整理的讨论内容。"
                " 先继续沟通，等你发送“总结一下”“整理待办”之类的指令时，我再统一输出。"
            )
            return self._deliver_reply(message, decision.mode, reply, analysis=None)

        analysis = self.orchestrator.run(
            AnalyzeRequest(
                session_id=message.session_id,
                raw_text=discussion_block,
            )
        )
        reply_preview = self._format_analysis_reply(analysis, decision.mode)
        self.memory_service.save_round(session_id=message.session_id, analysis=analysis)
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=analysis)

    def _handle_status_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict:
        tasks = self.memory_service.get_current_tasks(message.session_id)
        payload = self.memory_service.load_memory_payload(message.session_id)
        reply_preview = self._format_status_reply(message.text, tasks, payload)
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=None)

    def _handle_slides_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict:
        workspace_context = self.memory_service.build_workspace_context(message.session_id)
        if not workspace_context.strip():
            reply = (
                "我还没有拿到可用的讨论素材。"
                " 先在群里把目标、分工和结论聊出来，再让我生成演示稿大纲会更准确。"
            )
            return self._deliver_reply(message, decision.mode, reply, analysis=None)

        try:
            if self.llm_service.is_configured():
                outline = self.llm_service.generate_presentation_outline(
                    workspace_context,
                    message.text,
                )
            else:
                outline = self._build_fallback_outline(workspace_context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            outline = self._build_fallback_outline(workspace_context)

        reply_preview = f"【演示稿大纲】\n{outline}"
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=None)

    def _deliver_reply(
        self,
        message: FeishuMessageContext,
        mode: str,
        reply_preview: str | None,
        *,
        analysis: AnalyzeResponse | None,
    ) -> dict:
        reply_sent = False
        reply_error: str | None = None

        if reply_preview and settings.feishu_reply_enabled and message.chat_id:
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
            "session_id": message.session_id,
            "mode": mode,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
        }

    def _format_analysis_reply(self, analysis: AnalyzeResponse, mode: str) -> str:
        header = {
            "summary": "【讨论总结】",
            "tasks": "【待办清单】",
            "risks": "【风险与卡点】",
        }.get(mode, "【协作整理】")

        lines = [header, f"摘要：{analysis.summary}"]

        if mode in {"summary", "tasks"}:
            lines.append("任务：")
            if analysis.tasks:
                for idx, task in enumerate(analysis.tasks, start=1):
                    lines.append(
                        f"{idx}. {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 优先级：{task.priority}"
                    )
            else:
                lines.append("1. 当前讨论还没有形成明确待办。")

        if mode in {"summary", "risks"}:
            lines.append("风险：")
            if analysis.risks:
                for idx, risk in enumerate(analysis.risks, start=1):
                    lines.append(f"{idx}. {risk}")
            else:
                lines.append("1. 当前没有额外识别到新的显性风险。")

        lines.append("下一步建议：")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"{idx}. {action}")

        return "\n".join(lines)

    def _format_status_reply(self, query: str, tasks: list, payload: dict) -> str:
        if not tasks:
            return (
                "我这边还没有现成的任务快照。"
                " 你可以先让我“总结一下”或“整理待办”，我再基于那一轮结果回答状态问题。"
            )

        if "没负责人" in query or "未分配" in query:
            pending = [task for task in tasks if task.owner == "TBD"]
            if not pending:
                return "【负责人检查】\n当前任务都已经有负责人，没有未分配项。"
            lines = ["【负责人检查】", "以下任务还没有明确负责人："]
            for idx, task in enumerate(pending, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        if "谁负责" in query:
            lines = ["【当前分工】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} -> {task.owner}")
            return "\n".join(lines)

        if "截止" in query or "到期" in query:
            lines = ["【时间节点】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        if "风险" in query or "卡点" in query or "阻塞" in query:
            risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
            if not risks:
                risks = ["当前没有额外记录到新的风险项，但仍建议确认负责人和截止时间。"]
            lines = ["【当前风险】"]
            for idx, risk in enumerate(risks, start=1):
                lines.append(f"{idx}. {risk}")
            return "\n".join(lines)

        completed = sum(1 for task in tasks if str(task.status).lower() == "done")
        unassigned = sum(1 for task in tasks if task.owner == "TBD")
        lines = [
            "【当前协作状态】",
            f"- 任务总数：{len(tasks)}",
            f"- 已完成：{completed}",
            f"- 待确认负责人：{unassigned}",
            "- 如需更具体输出，可以继续发：整理待办 / 看风险 / 生成演示稿大纲",
        ]
        return "\n".join(lines)

    def _build_fallback_outline(self, workspace_context: str) -> str:
        return (
            "主题：基于飞书群聊讨论的协作推进方案\n"
            "适用场景：项目报名、路演准备、跨成员协同推进\n"
            "1. 项目背景与目标\n- 当前要解决什么问题\n- 为什么现在要推进\n"
            "2. 群聊讨论中的关键结论\n- 已经达成的一致意见\n- 当前范围与边界\n"
            "3. 任务拆解与角色分工\n- 谁负责什么\n- 关键时间节点\n"
            "4. 当前风险与待确认事项\n- 未定负责人\n- 未定截止时间\n"
            "5. 下一步推进计划\n- 本周动作\n- 演示前准备项\n"
            "演示时重点强调：这套流程以群聊讨论为入口，只在需要时触发 AI 整理和文稿生成，避免打断真实协作。"
        )
