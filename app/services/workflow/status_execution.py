from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.schemas.task_run import TaskRunDetail
from app.services.text_analysis import build_next_actions, extract_tasks, infer_risks, normalize_tasks
from app.services.workflow.document_tasks import task_items_from_llm_payload

logger = logging.getLogger(__name__)


class WorkflowStatusExecution:
    """Builds status answers and next-action snapshots for workflow steps."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_status_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        active_episode_id: int | None = None,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        next_action_reply = self.next_action_reply_for_status_query(message, task_run_id=task_run_id)
        if next_action_reply:
            return {
                "reply_preview": next_action_reply,
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            }
        clarification = workflow._pending_task_status_update_clarification_for_message(message)
        if clarification:
            return {
                "reply_preview": workflow.response_formatter.format_clarification_reply(
                    intent="tasks",
                    clarification=clarification,
                ),
                "analysis": None,
                "artifacts": [],
                "close_title": None,
            }
        tasks = workflow._context_tasks_for_message(message)
        payload = workflow._context_payload_for_message(message)
        status_answer = workflow.response_formatter.format_status_reply(message.text, tasks, payload)
        if active_episode_id is not None and tasks:
            self.persist_status_task_snapshot(
                message.session_id,
                tasks,
                episode_id=active_episode_id,
                source_message_id=message.message_id,
            )
        return {
            "reply_preview": status_answer,
            "analysis": None,
            "artifacts": [],
            "close_title": None,
        }
    def next_action_reply_for_status_query(self, message: FeishuMessageContext, *, task_run_id: str | None) -> str | None:
        workflow = self.workflow
        if not self.is_next_action_query(message.text):
            return None
        detail = workflow.task_run_service.get_task_run(task_run_id) if task_run_id else None
        recent_context = workflow._pending_discussion_text_for_message(message)
        if recent_context:
            reply = self.next_action_reply_for_pending_discussion(message, recent_context)
            if reply:
                return reply
        if detail is None:
            detail = self.synthetic_task_run_detail_for_message(message)
        bundle = workflow.next_action_service.build_for_task_run(detail, recent_context=recent_context or None)
        reply = workflow.response_formatter.format_next_action_block(bundle)
        if reply:
            return reply
        tasks = workflow._context_tasks_for_message(message)
        if not tasks:
            return None
        risks = infer_risks(tasks)
        next_actions = build_next_actions(tasks, risks)
        return self.format_task_snapshot_next_action_reply(tasks, risks, next_actions)

    def next_action_reply_for_pending_discussion(self, message: FeishuMessageContext, source_text: str) -> str | None:
        workflow = self.workflow
        if self.should_recommend_lifecycle_doc_from_discussion(source_text):
            return self.format_lifecycle_discussion_next_action_reply(source_text)
        tasks: list[TaskItem] = []
        risks: list[str] = []
        next_actions: list[str] = []
        if workflow.llm_service.is_configured():
            try:
                llm_result = workflow.llm_service.extract_collaboration(source_text)
                tasks = normalize_tasks(task_items_from_llm_payload(llm_result))
                risks = [str(item).strip() for item in llm_result.get("risks", []) if str(item).strip()]
                next_actions = [str(item).strip() for item in llm_result.get("next_actions", []) if str(item).strip()]
                logger.info(
                    "Next-action query inferred from pending discussion by LLM: session_id=%s tasks=%s risks=%s next_actions=%s",
                    message.session_id,
                    len(tasks),
                    len(risks),
                    len(next_actions),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM next-action inference failed, falling back to local parser: session_id=%s error=%s",
                    message.session_id,
                    exc,
                )
        if not tasks:
            tasks = normalize_tasks(extract_tasks(source_text))
        if not risks:
            risks = infer_risks(tasks)
        if not next_actions:
            next_actions = build_next_actions(tasks, risks)
        return self.format_pending_next_action_reply(tasks, risks, next_actions)

    @staticmethod
    def should_recommend_lifecycle_doc_from_discussion(source_text: str) -> bool:
        text = str(source_text or "").strip()
        if not text:
            return False
        task_markers = (
            "任务",
            "待办",
            "谁负责",
            "截止",
            "完成",
            "分工",
            "TODO",
            "todo",
        )
        lifecycle_markers = (
            "需求",
            "方案",
            "系统",
            "产品",
            "目标用户",
            "用户",
            "痛点",
            "核心流程",
            "流程",
            "范围",
            "一期",
            "二期",
            "风险",
            "审核",
            "报名",
            "数据",
            "老师",
            "学生",
        )
        lifecycle_hits = sum(1 for marker in lifecycle_markers if marker in text)
        task_hits = sum(1 for marker in task_markers if marker in text)
        return lifecycle_hits >= 2 and task_hits == 0

    @staticmethod
    def format_lifecycle_discussion_next_action_reply(source_text: str) -> str:
        risks = [
            line.strip("- 0123456789.、")
            for line in str(source_text or "").splitlines()
            if any(marker in line for marker in ("风险", "压力", "待确认", "需要确认", "规则"))
        ]
        lines = [
            "【基于当前讨论的下一步】",
            "建议优先做：",
            "1. 把本轮 IM 讨论整理成正式需求方案文档",
            "2. 在文档中固化背景痛点、目标用户、核心流程、一期范围和待确认风险",
            "3. 文档确认后继续生成汇报 PPT 或产品流程图",
        ]
        if risks:
            lines.append("需要留意：")
            for risk in risks[:3]:
                lines.append(f"- {risk}")
        return "\n".join(lines)

    @staticmethod
    def format_pending_next_action_reply(tasks: list[TaskItem], risks: list[str], next_actions: list[str]) -> str | None:
        cleaned_actions = [str(item).strip() for item in next_actions if str(item).strip()]
        cleaned_risks = [str(item).strip() for item in risks if str(item).strip()]
        if not cleaned_actions and not cleaned_risks and not tasks:
            return None
        lines = ["【基于当前讨论的下一步】"]
        if cleaned_actions:
            lines.append("建议优先做：")
            for index, action in enumerate(cleaned_actions[:4], start=1):
                lines.append(f"{index}. {action}")
        if cleaned_risks:
            lines.append("需要留意：")
            for risk in cleaned_risks[:3]:
                lines.append(f"- {risk}")
        if tasks:
            lines.append("关联任务：")
            for task in tasks[:5]:
                lines.append(f"- {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 状态：{task.status}")
        return "\n".join(lines)

    @staticmethod
    def format_task_snapshot_next_action_reply(tasks: list[TaskItem], risks: list[str], next_actions: list[str]) -> str | None:
        cleaned_actions = [str(item).strip() for item in next_actions if str(item).strip()]
        cleaned_risks = [str(item).strip() for item in risks if str(item).strip()]
        if not cleaned_actions and not cleaned_risks:
            return None
        lines = ["【基于当前任务的下一步】"]
        if cleaned_actions:
            lines.append("建议优先做：")
            for index, action in enumerate(cleaned_actions[:4], start=1):
                lines.append(f"{index}. {action}")
        if cleaned_risks:
            lines.append("需要留意：")
            for risk in cleaned_risks[:3]:
                lines.append(f"- {risk}")
        if tasks:
            lines.append("参考任务：")
            for task in tasks[:5]:
                lines.append(f"- {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 状态：{task.status}")
        return "\n".join(lines)

    @staticmethod
    def is_next_action_query(text: str) -> bool:
        query = str(text or "").strip().lower()
        if not query:
            return False
        return any(
            marker in query
            for marker in (
                "下一步",
                "下一步行动",
                "下一步建议",
                "接下来",
                "推荐",
                "next action",
                "next step",
            )
        )
    def synthetic_task_run_detail_for_message(self, message: FeishuMessageContext) -> TaskRunDetail:
        workflow = self.workflow
        try:
            payloads = workflow.session_document_service.list_documents(message.session_id)
            documents = [workflow.task_run_service._session_document_from_payload(item) for item in payloads]
        except Exception:
            documents = []
        return TaskRunDetail(
            task_run_id=f"synthetic_{message.message_id or message.session_id}",
            session_id=message.session_id,
            source_type=getattr(message, "chat_type", None) or "unknown",
            source_ref=getattr(message, "chat_id", None),
            trigger_message_id=getattr(message, "message_id", None),
            intent="status",
            title="上下文下一步行动推荐",
            stage="recommendation",
            status="completed",
            latest_summary="",
            latest_reply_preview="",
            latest_error=None,
            session_documents=documents,
        )
    def persist_status_task_snapshot(
        self,
        session_id: str,
        tasks: list,
        *,
        episode_id: int | None,
        source_message_id: str | None = None,
    ) -> None:
        workflow = self.workflow
        normalized_tasks: list[TaskItem] = []
        for task in tasks:
            if isinstance(task, TaskItem):
                normalized_tasks.append(task)
                continue
            try:
                normalized_tasks.append(TaskItem.model_validate(task))
            except Exception:
                continue
        if not normalized_tasks:
            return
        risks = infer_risks(normalized_tasks)
        analysis = AnalyzeResponse(
            session_id=session_id,
            summary="根据当前文档和最新讨论刷新任务快照。",
            tasks=normalized_tasks,
            risks=risks,
            next_actions=build_next_actions(normalized_tasks, risks),
            agent_traces=[AgentTrace(agent="status", summary="persisted task snapshot from status query")],
        )
        try:
            workflow.memory_service.save_round(
                session_id=session_id,
                analysis=analysis,
                episode_id=episode_id,
                source_message_id=source_message_id,
                embed=False,
                async_embed=False,
                preserve_unmatched_previous=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist status task snapshot: session_id=%s error=%s", session_id, exc)
