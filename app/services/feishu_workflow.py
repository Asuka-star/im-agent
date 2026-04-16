import logging
from typing import Any

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.bitable_api import FeishuBitableAPI
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
        self.bitable_api = FeishuBitableAPI()
        self.memory_service = MemoryService()
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()

    def handle_message(self, message: FeishuMessageContext) -> dict[str, Any]:
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
            return self._empty_result(message.session_id, decision.mode)

        if decision.mode in {"summary", "tasks", "risks", "bitable"}:
            result = self._handle_analysis_trigger(message, decision)
        elif decision.mode == "status":
            result = self._handle_status_trigger(message, decision)
        elif decision.mode == "slides":
            result = self._handle_slides_trigger(message, decision)
        else:
            result = self._empty_result(message.session_id, "buffer")

        if result["reply_preview"]:
            self.memory_service.save_assistant_message(
                session_id=message.session_id,
                content=result["reply_preview"],
            )

        return result

    def _empty_result(self, session_id: str, mode: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "mode": mode,
            "reply_preview": None,
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
        }

    def _handle_analysis_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict[str, Any]:
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
        sync_lines: list[str] = []
        if decision.mode == "bitable":
            sync_lines = self._sync_tasks_to_bitable(analysis, message.session_id)

        reply_preview = self._format_analysis_reply(analysis, decision.mode, sync_lines)
        self.memory_service.save_round(session_id=message.session_id, analysis=analysis)
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=analysis)

    def _handle_status_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict[str, Any]:
        tasks = self.memory_service.get_current_tasks(message.session_id)
        payload = self.memory_service.load_memory_payload(message.session_id)
        reply_preview = self._format_status_reply(message.text, tasks, payload)
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=None)

    def _handle_slides_trigger(
        self,
        message: FeishuMessageContext,
        decision: InteractionDecision,
    ) -> dict[str, Any]:
        workspace_context = self.memory_service.build_workspace_context(message.session_id)
        if not workspace_context.strip():
            reply = (
                "我还没有拿到可用的讨论素材。"
                " 先在群里把目标、分工和结论聊出来，再让我生成演示稿大纲会更准确。"
            )
            return self._deliver_reply(message, decision.mode, reply, analysis=None)

        try:
            if self.llm_service.is_configured():
                package = self.llm_service.generate_presentation_package(
                    workspace_context,
                    message.text,
                )
            else:
                package = self._build_fallback_presentation_package(message.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            package = self._build_fallback_presentation_package(message.session_id)

        reply_preview = self._format_presentation_reply(package)
        return self._deliver_reply(message, decision.mode, reply_preview, analysis=None)

    def _deliver_reply(
        self,
        message: FeishuMessageContext,
        mode: str,
        reply_preview: str | None,
        *,
        analysis: AnalyzeResponse | None,
    ) -> dict[str, Any]:
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

    def _format_analysis_reply(
        self,
        analysis: AnalyzeResponse,
        mode: str,
        sync_lines: list[str] | None = None,
    ) -> str:
        header = {
            "summary": "【讨论总结】",
            "tasks": "【待办清单】",
            "risks": "【风险与卡点】",
            "bitable": "【待办清单 + 表格同步】",
        }.get(mode, "【协作整理】")

        lines = [header, f"摘要：{analysis.summary}"]

        if mode in {"summary", "tasks", "bitable"}:
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

        if sync_lines:
            lines.append("表格同步：")
            lines.extend(sync_lines)

        return "\n".join(lines)

    def _sync_tasks_to_bitable(self, analysis: AnalyzeResponse, session_id: str) -> list[str]:
        if not analysis.tasks:
            return ["- 当前没有可同步的任务记录。"]
        if not self.bitable_api.is_configured():
            return ["- 多维表格未配置，暂未执行写入。"]

        created = 0
        failed: list[str] = []
        for task in analysis.tasks:
            try:
                self.bitable_api.create_task_record(task, session_id=session_id)
                created += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"- 《{task.title}》写入失败：{exc}")

        summary = [f"- 成功写入 {created} 条任务到多维表格。"]
        return summary + failed

    def _format_status_reply(self, query: str, tasks: list, payload: dict[str, Any]) -> str:
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
        return "\n".join(
            [
                "【当前协作状态】",
                f"- 任务总数：{len(tasks)}",
                f"- 已完成：{completed}",
                f"- 待确认负责人：{unassigned}",
                "- 如需更具体输出，可以继续发：整理待办 / 看风险 / 生成演示稿大纲",
            ]
        )

    def _format_presentation_reply(self, package: dict[str, Any]) -> str:
        theme = str(package.get("theme") or "基于群聊讨论的协作汇报").strip()
        audience = str(package.get("audience") or "项目汇报 / 路演准备").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        lines = [
            "【演示稿大纲】",
            f"主题：{theme}",
            f"适用场景：{audience}",
        ]

        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"第{index}页").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            lines.append(f"P{index}. {title}")
            for bullet in bullets[:4]:
                lines.append(f"- {str(bullet).strip()}")

        if emphasis:
            lines.append("演示时重点强调：")
            for item in emphasis[:3]:
                lines.append(f"- {str(item).strip()}")

        if assets:
            lines.append("建议补充素材：")
            for item in assets[:4]:
                lines.append(f"- {str(item).strip()}")

        return "\n".join(lines)

    def _build_fallback_presentation_package(self, session_id: str) -> dict[str, Any]:
        tasks = self.memory_service.get_current_tasks(session_id)
        payload = self.memory_service.load_memory_payload(session_id)

        task_lines = [
            f"{task.title}（负责人：{task.owner}，截止：{task.due_date}）"
            for task in tasks[:4]
        ] or ["明确项目目标、角色分工与时间节点"]

        risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
        next_actions = payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else []

        return {
            "theme": "基于飞书群聊讨论的协作推进方案",
            "audience": "项目报名、路演准备、团队协同推进",
            "slides": [
                {
                    "title": "项目背景与目标",
                    "bullets": [
                        "当前要解决的核心问题是什么",
                        "为什么需要用 AI 协助办公协同",
                        "这次输出服务于什么汇报或报名场景",
                    ],
                },
                {
                    "title": "讨论中形成的关键结论",
                    "bullets": task_lines[:3],
                },
                {
                    "title": "任务拆解与分工",
                    "bullets": task_lines,
                },
                {
                    "title": "当前风险与待确认事项",
                    "bullets": risks[:3] or ["负责人和截止时间仍需进一步确认"],
                },
                {
                    "title": "下一步推进计划",
                    "bullets": next_actions[:3] or ["继续在群里同步进展并更新协作视图"],
                },
            ],
            "emphasis": [
                "AI 不打断日常讨论，而是在需要时统一整理和输出",
                "协作结果可以从 IM 继续延展到文档和演示稿",
            ],
            "assets": [
                "最新任务清单截图",
                "关键讨论结论摘要",
                "时间线或里程碑信息",
            ],
        }
