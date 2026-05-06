from __future__ import annotations

import logging
from typing import Any

from app.schemas.analyze import AnalyzeRequest
from app.schemas.feishu_event import FeishuMessageContext
from app.services.artifact_title_service import ArtifactTitleService

logger = logging.getLogger(__name__)


class WorkflowFallbackHandler:
    """Handles deterministic/local fallbacks when deep LLM planning is unavailable."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def handle_fallback_request(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        decision = workflow.interaction_service.decide(message.text)
        if task_run_id:
            workflow.task_run_service.update_task_run(
                task_run_id,
                intent=decision.mode,
                title=workflow._task_run_title(message.text, decision.mode),
                stage=f"{decision.mode}_fallback",
            )

        if decision.mode == "help":
            return workflow.reply_sender.deliver_reply(message, "help", workflow.response_formatter.format_help_reply(), analysis=None)

        if decision.mode == "slides":
            return self.handle_fallback_slides(message, active_episode_id, task_run_id=task_run_id)
        if decision.mode == "doc":
            return self.handle_fallback_doc(message, active_episode_id, task_run_id=task_run_id)
        if decision.mode == "canvas":
            return self.handle_fallback_canvas(message, active_episode_id, task_run_id=task_run_id)

        if decision.mode == "status":
            tasks = workflow._context_tasks_for_message(message)
            payload = workflow._context_payload_for_message(message)
            if active_episode_id is not None and tasks:
                workflow.status_execution.persist_status_task_snapshot(
                    message.session_id,
                    tasks,
                    episode_id=active_episode_id,
                    source_message_id=message.message_id,
                )
            reply_preview = workflow.response_formatter.format_status_reply(message.text, tasks, payload)
            return workflow.reply_sender.deliver_reply(message, "status", reply_preview, analysis=None, episode_id=active_episode_id)

        discussion_block = workflow.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        )
        if not discussion_block:
            reply = "我已经开始旁听这轮讨论了。你继续聊，等需要的时候再 @我整理需求方案文档或生成正式演示稿。"
            return workflow.reply_sender.deliver_reply(message, "help", reply, analysis=None)

        analysis = workflow.orchestrator.run(
            AnalyzeRequest(session_id=message.session_id, raw_text=discussion_block)
        )
        reply_preview = workflow.response_formatter.format_analysis_reply(analysis, decision.mode)
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            source_message_id=message.message_id,
            async_embed=True,
        )
        result = workflow.reply_sender.deliver_reply(message, decision.mode, reply_preview, analysis=analysis, episode_id=active_episode_id)
        if active_episode_id is not None and workflow._should_close_episode(result, reply_preview):
            workflow.memory_service.close_active_episode(message.session_id, title=analysis.summary)
        return result

    def handle_fallback_slides(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        workspace_context = workflow._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        if not workspace_context.strip():
            reply = "我这边还没有拿到可用的讨论素材。先在群里把需求背景、目标用户、核心方案和关键结论聊出来，再让我生成正式演示稿会更准确。"
            return workflow.reply_sender.deliver_reply(message, "slides", reply, analysis=None)

        provider = "llm"
        try:
            package = workflow.llm_service.generate_presentation_package(workspace_context, message.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            package = self.build_fallback_presentation_package(message.session_id)
            provider = "fallback"

        package = self._with_semantic_presentation_title(
            package,
            instruction=message.text,
            workspace_context=workspace_context,
        )
        presentation_tool = workflow._presentation_tool()
        artifact = presentation_tool.persist_artifact(
            package,
            provider=provider,
            session_id=message.session_id,
            task_run_id=task_run_id,
        )
        reply_preview = presentation_tool.format_reply(package, artifact=artifact)
        result = workflow.reply_sender.deliver_reply(
            message,
            "slides",
            reply_preview,
            analysis=None,
            episode_id=active_episode_id,
            artifacts=[artifact],
            task_run_id=task_run_id,
        )
        if active_episode_id is not None and workflow._should_close_episode(result, reply_preview):
            workflow.memory_service.close_active_episode(message.session_id, title="slides")
        return result

    def handle_fallback_canvas(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        try:
            workspace_context = workflow._build_workspace_context_for_message(
                message,
                active_episode_id=active_episode_id,
                include_semantic_search=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Canvas fallback context loading failed, using instruction only: %s", exc)
            workspace_context = ""
        step_result = workflow.canvas_execution.prepare_canvas_execution(
            message,
            llm_result={},
            workspace_context=workspace_context,
            task_run_id=task_run_id,
        )
        result = workflow.reply_sender.deliver_reply(
            message,
            "canvas",
            step_result.get("reply_preview"),
            analysis=None,
            episode_id=active_episode_id,
            artifacts=step_result.get("artifacts", []),
            task_run_id=task_run_id,
        )
        if active_episode_id is not None and workflow._should_close_episode(result, str(step_result.get("reply_preview") or "")):
            workflow.memory_service.close_active_episode(message.session_id, title=str(step_result.get("close_title") or "canvas"))
        return result

    def handle_fallback_doc(
        self,
        message: FeishuMessageContext,
        active_episode_id: int | None,
        *,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        workspace_context = workflow._build_workspace_context_for_message(
            message,
            active_episode_id=active_episode_id,
            include_semantic_search=False,
        )
        package, analysis = workflow.doc_execution.build_doc_response_package(
            session_id=message.session_id,
            instruction=message.text,
            llm_result={},
            workspace_context=workspace_context,
            episode_id=active_episode_id,
            reason="为文档同步生成结构化沉淀",
            source_message_id=message.message_id,
        )
        sync_result = workflow.doc_execution.sync_package_to_session_doc(
            package,
            session_id=message.session_id,
            episode_id=active_episode_id,
            instruction=message.text,
            task_run_id=task_run_id,
        )
        artifact = workflow.doc_execution.build_document_artifact(
            session_id=message.session_id,
            package=package,
            sync_result=sync_result,
            fallback_provider="fallback",
            task_run_id=task_run_id,
        )
        sync_lines = sync_result.summary_lines
        reply_preview = workflow.doc_execution.format_doc_reply(package, sync_lines)
        result = workflow.reply_sender.deliver_reply(
            message,
            "doc",
            reply_preview,
            analysis=analysis,
            episode_id=active_episode_id,
            artifacts=[artifact],
            task_run_id=task_run_id,
        )
        if active_episode_id is not None and workflow._should_close_episode(result, reply_preview):
            workflow.memory_service.close_active_episode(message.session_id, title=str(package.get("title") or "doc"))
        return result

    @staticmethod
    def _with_semantic_presentation_title(package: dict, *, instruction: str, workspace_context: str) -> dict:
        title = ArtifactTitleService.presentation_title(
            current_title=str(package.get("theme") or "").strip(),
            instruction=instruction,
            workspace_context=workspace_context,
        )
        if not title:
            return package
        package = dict(package)
        package["theme"] = title
        return package

    def build_fallback_presentation_package(self, session_id: str) -> dict:
        workflow = self.workflow
        try:
            tasks = workflow.memory_service.get_current_tasks(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load tasks for fallback presentation package: %s", exc)
            tasks = []
        try:
            payload = workflow.memory_service.load_memory_payload(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load memory payload for fallback presentation package: %s", exc)
            payload = {}

        task_lines = [
            f"{task.title}（负责人：{task.owner}，截止：{task.due_date}）"
            for task in tasks[:4]
        ] or ["明确项目目标、方案范围与关键里程碑"]

        risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
        next_actions = payload.get("next_actions") if isinstance(payload.get("next_actions"), list) else []

        return {
            "theme": "基于飞书群聊讨论的需求方案与正式汇报",
            "audience": "需求评审、项目答辩、路演准备",
            "slides": [
                {
                    "title": "需求背景与核心痛点",
                    "bullets": [
                        "当前要解决的核心问题是什么",
                        "目标用户在现有流程中遇到什么阻塞",
                        "这次方案要沉淀为可汇报、可落地的成果",
                    ],
                },
                {
                    "title": "核心需求与方案范围",
                    "bullets": next_actions[:3] or ["明确主要用户、核心功能、边界和二期能力"],
                },
                {
                    "title": "产品流程与技术方案",
                    "bullets": [
                        "从 IM 讨论沉淀需求，再进入文档、流程图和演示稿",
                        "用结构化运行态记录 Agent 的理解、生成、确认和交付过程",
                        "关键产物可在 Workbench 中预览、修订和打包",
                    ],
                },
                {
                    "title": "当前风险与待确认事项",
                    "bullets": risks[:3] or ["需求边界、权限配置和演示稳定性仍需复核"],
                },
                {
                    "title": "实施计划与分工",
                    "bullets": task_lines,
                },
            ],
            "emphasis": [
                "主线是 IM 需求讨论到正式文档与演示文稿的沉淀",
                "任务分工只是需求落地计划的一部分，不是整体汇报的主角",
            ],
            "assets": ["需求文档截图", "流程图或架构图", "交付包验收清单"],
        }
