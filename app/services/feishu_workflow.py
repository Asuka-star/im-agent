import logging
import time

from app.agents.orchestrator import AgentOrchestrator
from app.core.config import settings
from app.feishu.bitable_api import FeishuBitableAPI
from app.feishu.doc_api import FeishuDocAPI
from app.feishu.message_api import FeishuMessageAPI
from app.feishu.user_api import FeishuUserAPI
from app.schemas.analyze import AgentTrace, AnalyzeRequest, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.interaction import InteractionService
from app.services.llm import LLMService
from app.services.memory_service import MemoryService
from app.services.text_analysis import (
    apply_discussion_updates,
    build_next_actions,
    build_summary,
    infer_risks,
    normalize_tasks,
)

logger = logging.getLogger(__name__)


class FeishuWorkflowService:
    """Handles buffered collaboration and LLM-first mentioned requests."""

    def __init__(self) -> None:
        self.orchestrator = AgentOrchestrator()
        self.message_api = FeishuMessageAPI()
        self.bitable_api = FeishuBitableAPI()
        self.doc_api = FeishuDocAPI()
        self.user_api = FeishuUserAPI()
        self.memory_service = MemoryService()
        self.interaction_service = InteractionService()
        self.llm_service = LLMService()

    def handle_message(self, message: FeishuMessageContext) -> dict:
        started_at = time.perf_counter()
        active_episode_id: int | None = None
        if message.chat_type == "group" and not message.is_mentioned:
            active_episode = self.memory_service.ensure_active_episode(message.session_id)
            active_episode_id = active_episode.id

        self._ensure_sender_alias(message)

        self.memory_service.save_user_message(
            session_id=message.session_id,
            message_id=message.message_id,
            sender_id=message.sender_id,
            content=message.text or message.raw_text,
            episode_id=active_episode_id,
            mentioned_users=[user.model_dump() for user in message.mentioned_users],
            embed=False,
        )
        logger.info(
            "Workflow stage completed: message_id=%s stage=save_user_message elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )

        if message.chat_type == "group" and not message.is_mentioned:
            logger.info(
                "Workflow stage completed: message_id=%s stage=buffer_return total_elapsed_ms=%.1f",
                message.message_id,
                (time.perf_counter() - started_at) * 1000,
            )
            return self._empty_result(message.session_id, "buffer")

        result = self._handle_mentioned_request(message)
        if result["reply_preview"]:
            self.memory_service.save_assistant_message(
                session_id=message.session_id,
                content=result["reply_preview"],
                episode_id=result.get("episode_id"),
                embed=False,
            )
        logger.info(
            "Workflow stage completed: message_id=%s stage=workflow_done total_elapsed_ms=%.1f",
            message.message_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return result

    def _ensure_sender_alias(self, message: FeishuMessageContext) -> None:
        if self.memory_service.get_alias_display_name(message.session_id, message.sender_id):
            return

        display_name = None
        try:
            display_name = self.user_api.get_user_display_name(
                user_id=message.sender_user_id,
                open_id=message.sender_open_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve sender display name for %s: %s", message.sender_id, exc)

        if not display_name:
            return

        self.memory_service.upsert_user_alias(
            message.session_id,
            display_name=display_name,
            user_id=message.sender_user_id,
            open_id=message.sender_open_id,
            union_id=message.sender_union_id,
        )

    def _handle_mentioned_request(self, message: FeishuMessageContext) -> dict:
        active_episode = self.memory_service.get_active_episode(message.session_id)
        active_episode_id = active_episode.id if active_episode else None
        base_workspace_context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            include_semantic_search=False,
            episode_id=active_episode_id,
        )
        workspace_context = base_workspace_context

        if self.llm_service.is_configured():
            try:
                memory_gate = self.llm_service.should_recall_memories(base_workspace_context, message.text)
                if bool(memory_gate.get("should_recall")):
                    workspace_context = self.memory_service.build_workspace_context(
                        message.session_id,
                        include_pending=True,
                        exclude_message_id=message.message_id,
                        query_text=message.text,
                        include_semantic_search=True,
                        episode_id=active_episode_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Memory gate failed, continuing without semantic recall: %s", exc)

        if self.llm_service.is_configured():
            try:
                llm_result = self.llm_service.resolve_workspace_request(workspace_context, message.text)
                return self._execute_llm_request(message, llm_result, workspace_context, active_episode_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unified workspace request failed, falling back: %s", exc)

        return self._handle_fallback_request(message, active_episode_id)

    def _execute_llm_request(
        self,
        message: FeishuMessageContext,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
    ) -> dict:
        intent = str(llm_result.get("intent") or "").strip().lower()
        reason = str(llm_result.get("reason") or "").strip()

        if intent in {"help", "unknown", ""}:
            return self._deliver_reply(message, "help", self._format_help_reply(reason), analysis=None)

        if intent == "slides":
            package = llm_result.get("slides")
            if not isinstance(package, dict) or not package.get("slides"):
                try:
                    package = self.llm_service.generate_presentation_package(workspace_context, message.text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Slide package fallback generation failed: %s", exc)
                    package = self._build_fallback_presentation_package(message.session_id)
            reply_preview = self._format_presentation_reply(package)
            result = self._deliver_reply(message, "slides", reply_preview, analysis=None, episode_id=active_episode_id)
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title="slides")
            return result

        if intent == "doc":
            package = llm_result.get("doc")
            if not isinstance(package, dict) or not package.get("sections"):
                package = self._build_document_package_from_workspace(
                    session_id=message.session_id,
                    instruction=message.text,
                    llm_result=llm_result,
                    workspace_context=workspace_context,
                    episode_id=active_episode_id,
                )
            sync_lines = self._sync_package_to_doc(package)
            reply_preview = self._format_doc_reply(package, sync_lines)
            result = self._deliver_reply(message, "doc", reply_preview, analysis=None, episode_id=active_episode_id)
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title=str(package.get("title") or "doc"))
            return result

        if intent == "status":
            status_answer = str(llm_result.get("status_answer") or "").strip()
            if not status_answer:
                tasks = self.memory_service.get_current_tasks(message.session_id)
                payload = self.memory_service.load_memory_payload(message.session_id)
                status_answer = self._format_status_reply(message.text, tasks, payload)
            return self._deliver_reply(message, "status", status_answer, analysis=None)

        if intent in {"summary", "tasks", "risks", "bitable"}:
            source_text = self.memory_service.build_discussion_block(
                message.session_id,
                episode_id=active_episode_id,
                exclude_message_id=message.message_id,
            ) or workspace_context or message.text
            analysis = self._build_analysis_from_llm(
                session_id=message.session_id,
                source_text=source_text,
                llm_result=llm_result,
                intent=intent,
                reason=reason,
            )
            sync_lines: list[str] = []
            if intent == "bitable":
                sync_lines = self._sync_tasks_to_bitable(analysis, message.session_id)
            reply_preview = self._format_analysis_reply(analysis, intent, sync_lines)
            self.memory_service.save_round(
                session_id=message.session_id,
                analysis=analysis,
                episode_id=active_episode_id,
                async_embed=True,
            )
            result = self._deliver_reply(message, intent, reply_preview, analysis=analysis, episode_id=active_episode_id)
            if active_episode_id is not None and self._should_close_episode(result, reply_preview):
                self.memory_service.close_active_episode(message.session_id, title=analysis.summary)
            return result

        return self._deliver_reply(message, "help", self._format_help_reply(reason), analysis=None)

    def _build_analysis_from_llm(
        self,
        *,
        session_id: str,
        source_text: str,
        llm_result: dict,
        intent: str,
        reason: str,
    ) -> AnalyzeResponse:
        llm_tasks = [
            TaskItem.model_validate(item)
            for item in llm_result.get("tasks", [])
            if isinstance(item, dict)
        ]
        current_tasks = self._current_task_items(session_id)
        task_operations = llm_result.get("task_operations", [])

        if isinstance(task_operations, list) and task_operations:
            tasks = self._apply_llm_task_operations(current_tasks, task_operations)
        elif llm_tasks:
            tasks = llm_tasks
        else:
            tasks = apply_discussion_updates(current_tasks, source_text)

        tasks = normalize_task_dates(normalize_tasks(tasks))

        summary = str(llm_result.get("summary") or "").strip() or build_summary(source_text, tasks)
        risks = [str(item).strip() for item in llm_result.get("risks", []) if str(item).strip()]
        if not risks:
            risks = infer_risks(tasks)

        next_actions = [str(item).strip() for item in llm_result.get("next_actions", []) if str(item).strip()]
        if not next_actions:
            next_actions = build_next_actions(tasks, risks)

        traces = [
            AgentTrace(
                agent="router",
                summary=f"LLM reviewed the full workspace context and chose mode={intent}. {reason}".strip(),
            ),
            AgentTrace(
                agent="planner",
                summary=(
                    f"LLM returned {len(tasks)} refreshed task(s)"
                    f" and {len(task_operations) if isinstance(task_operations, list) else 0} task operation(s)"
                    " after considering the whole discussion."
                ),
            ),
            AgentTrace(
                agent="coordinator",
                summary=f"Normalized {len(tasks)} task(s), including date cleanup and field normalization.",
            ),
            AgentTrace(
                agent="reviewer",
                summary=f"Generated {len(risks)} risk signal(s) and {len(next_actions)} next action(s).",
            ),
            AgentTrace(
                agent="memory",
                summary=f"Prepared this round for persistence: {summary}",
            ),
        ]

        return AnalyzeResponse(
            session_id=session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=next_actions[:4],
            agent_traces=traces,
        )

    def _current_task_items(self, session_id: str) -> list[TaskItem]:
        rows = self.memory_service.get_current_tasks(session_id)
        return [
            TaskItem(
                title=row.title,
                owner=row.owner,
                priority=row.priority,
                due_date=row.due_date,
                status=row.status,
                notes=row.notes or "",
            )
            for row in rows
        ]

    def _apply_llm_task_operations(self, current_tasks: list[TaskItem], operations: list[dict]) -> list[TaskItem]:
        refreshed = [task.model_copy(deep=True) for task in current_tasks]
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "").strip().lower()
            match_hint = operation.get("match_hint") if isinstance(operation.get("match_hint"), dict) else {}
            task_payload = operation.get("task") if isinstance(operation.get("task"), dict) else None

            if action == "create" and task_payload:
                refreshed.append(TaskItem.model_validate(task_payload))
                continue

            target_index = self._find_operation_target(refreshed, match_hint, task_payload)
            if target_index is None:
                if action == "create" and task_payload:
                    refreshed.append(TaskItem.model_validate(task_payload))
                continue

            if action == "remove":
                refreshed.pop(target_index)
                continue

            if action == "update" and task_payload:
                refreshed[target_index] = TaskItem.model_validate(task_payload)

        return refreshed

    def _find_operation_target(
        self,
        tasks: list[TaskItem],
        match_hint: dict,
        task_payload: dict | None,
    ) -> int | None:
        hint_title = str(match_hint.get("title") or "").strip()
        hint_owner = str(match_hint.get("owner") or "").strip()
        payload_title = str(task_payload.get("title") or "").strip() if task_payload else ""
        payload_owner = str(task_payload.get("owner") or "").strip() if task_payload else ""

        def normalized(value: str) -> str:
            return " ".join(value.lower().split())

        candidates: list[tuple[str, str]] = []
        if hint_title or hint_owner:
            candidates.append((hint_title, hint_owner))
        if payload_title or payload_owner:
            candidates.append((payload_title, payload_owner))

        for title, owner in candidates:
            exact_matches = [
                idx
                for idx, task in enumerate(tasks)
                if (not title or normalized(task.title) == normalized(title))
                and (not owner or normalized(task.owner) == normalized(owner))
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]

        for title, _ in candidates:
            if not title:
                continue
            title_matches = [idx for idx, task in enumerate(tasks) if normalized(task.title) == normalized(title)]
            if len(title_matches) == 1:
                return title_matches[0]

        return None

    def _handle_fallback_request(self, message: FeishuMessageContext, active_episode_id: int | None) -> dict:
        decision = self.interaction_service.decide(message.text)

        if decision.mode == "help":
            return self._deliver_reply(message, "help", self._format_help_reply(), analysis=None)

        if decision.mode == "slides":
            return self._handle_fallback_slides(message, active_episode_id)
        if decision.mode == "doc":
            return self._handle_fallback_doc(message, active_episode_id)

        if decision.mode == "status":
            tasks = self.memory_service.get_current_tasks(message.session_id)
            payload = self.memory_service.load_memory_payload(message.session_id)
            reply_preview = self._format_status_reply(message.text, tasks, payload)
            return self._deliver_reply(message, "status", reply_preview, analysis=None)

        discussion_block = self.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        )
        if not discussion_block:
            reply = "我已经开始旁听这轮讨论了。你继续聊，等需要的时候再 @我做总结、整理待办或生成汇报大纲。"
            return self._deliver_reply(message, "help", reply, analysis=None)

        analysis = self.orchestrator.run(
            AnalyzeRequest(session_id=message.session_id, raw_text=discussion_block)
        )
        sync_lines: list[str] = []
        if decision.mode == "bitable":
            sync_lines = self._sync_tasks_to_bitable(analysis, message.session_id)
        reply_preview = self._format_analysis_reply(analysis, decision.mode, sync_lines)
        self.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            async_embed=True,
        )
        result = self._deliver_reply(message, decision.mode, reply_preview, analysis=analysis, episode_id=active_episode_id)
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title=analysis.summary)
        return result

    def _handle_fallback_slides(self, message: FeishuMessageContext, active_episode_id: int | None) -> dict:
        workspace_context = self.memory_service.build_workspace_context(
            message.session_id,
            include_pending=True,
            exclude_message_id=message.message_id,
            query_text=message.text,
            episode_id=active_episode_id,
        )
        if not workspace_context.strip():
            reply = "我这边还没有拿到可用的讨论素材。先在群里把目标、分工和结论聊出来，再让我生成汇报大纲会更准确。"
            return self._deliver_reply(message, "slides", reply, analysis=None)

        try:
            package = self.llm_service.generate_presentation_package(workspace_context, message.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slide outline generation failed, falling back to template: %s", exc)
            package = self._build_fallback_presentation_package(message.session_id)

        reply_preview = self._format_presentation_reply(package)
        result = self._deliver_reply(message, "slides", reply_preview, analysis=None, episode_id=active_episode_id)
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title="slides")
        return result

    def _handle_fallback_doc(self, message: FeishuMessageContext, active_episode_id: int | None) -> dict:
        package = self._build_document_package_from_workspace(
            session_id=message.session_id,
            instruction=message.text,
            llm_result={},
            workspace_context=self.memory_service.build_workspace_context(
                message.session_id,
                include_pending=True,
                exclude_message_id=message.message_id,
                query_text=message.text,
                episode_id=active_episode_id,
            ),
            episode_id=active_episode_id,
        )
        sync_lines = self._sync_package_to_doc(package)
        reply_preview = self._format_doc_reply(package, sync_lines)
        result = self._deliver_reply(message, "doc", reply_preview, analysis=None, episode_id=active_episode_id)
        if active_episode_id is not None and self._should_close_episode(result, reply_preview):
            self.memory_service.close_active_episode(message.session_id, title=str(package.get("title") or "doc"))
        return result

    def _build_document_package_from_workspace(
        self,
        *,
        session_id: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        episode_id: int | None,
    ) -> dict:
        provided = llm_result.get("doc")
        if isinstance(provided, dict) and isinstance(provided.get("sections"), list) and provided.get("sections"):
            title = str(provided.get("title") or "").strip() or self._default_doc_title(instruction)
            return {
                "title": title,
                "sections": provided.get("sections", []),
            }

        wants_outline = any(keyword in instruction for keyword in ("汇报", "路演", "大纲", "PPT", "ppt", "演示"))
        if wants_outline:
            slides = llm_result.get("slides")
            if not isinstance(slides, dict) or not slides.get("slides"):
                try:
                    slides = self.llm_service.generate_presentation_package(workspace_context, instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Document fallback slide generation failed: %s", exc)
                    slides = self._build_fallback_presentation_package(session_id)
            return self._document_from_presentation(slides, instruction)

        source_text = self.memory_service.build_discussion_block(
            session_id,
            episode_id=episode_id,
        ) or workspace_context or instruction
        analysis = self._build_analysis_from_llm(
            session_id=session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent="summary",
            reason="为文档同步生成结构化沉淀",
        )
        return self._document_from_analysis(analysis, instruction)

    def _document_from_analysis(self, analysis: AnalyzeResponse, instruction: str) -> dict:
        sections = [
            {
                "heading": "讨论摘要",
                "paragraphs": [analysis.summary],
            }
        ]
        if analysis.tasks:
            sections.append(
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        f"{idx}. {task.title}｜负责人：{task.owner}｜截止：{task.due_date}｜优先级：{task.priority}"
                        for idx, task in enumerate(analysis.tasks, start=1)
                    ],
                }
            )
        if analysis.risks:
            sections.append(
                {
                    "heading": "风险与卡点",
                    "paragraphs": [f"{idx}. {risk}" for idx, risk in enumerate(analysis.risks, start=1)],
                }
            )
        if analysis.next_actions:
            sections.append(
                {
                    "heading": "下一步建议",
                    "paragraphs": [f"{idx}. {item}" for idx, item in enumerate(analysis.next_actions, start=1)],
                }
            )
        return {
            "title": self._default_doc_title(instruction),
            "sections": sections,
        }

    def _document_from_presentation(self, package: dict, instruction: str) -> dict:
        theme = str(package.get("theme") or "汇报大纲").strip()
        audience = str(package.get("audience") or "团队协作汇报").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        sections = [
            {
                "heading": "文档说明",
                "paragraphs": [f"主题：{theme}", f"适用场景：{audience}"],
            }
        ]
        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"P{index}").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            sections.append(
                {
                    "heading": f"P{index}. {title}",
                    "paragraphs": [str(item).strip() for item in bullets if str(item).strip()],
                }
            )
        if emphasis:
            sections.append(
                {
                    "heading": "演示重点",
                    "paragraphs": [str(item).strip() for item in emphasis if str(item).strip()],
                }
            )
        if assets:
            sections.append(
                {
                    "heading": "建议补充素材",
                    "paragraphs": [str(item).strip() for item in assets if str(item).strip()],
                }
            )
        return {
            "title": self._default_doc_title(instruction, fallback=theme),
            "sections": sections,
        }

    def _sync_package_to_doc(self, package: dict) -> list[str]:
        if not self.doc_api.is_configured():
            return ["- 飞书文档未配置，暂未执行创建。"]

        try:
            created = self.doc_api.create_document_from_sections(
                str(package.get("title") or "协同文档"),
                package.get("sections") if isinstance(package.get("sections"), list) else [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu doc sync failed: %s", exc)
            return [f"- 文档创建失败：{exc}"]

        lines = [f"- 已创建飞书文档：《{created['title']}》"]
        if created.get("url"):
            lines.append(f"- 文档链接：{created['url']}")
        return lines

    def _format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        lines = ["【文档同步】", f"标题：{str(package.get('title') or '协同文档').strip()}"]
        if sections:
            lines.append("正文结构：")
            for index, section in enumerate(sections[:6], start=1):
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("heading") or f"部分 {index}").strip()
                paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
                lines.append(f"{index}. {heading}")
                for paragraph in paragraphs[:2]:
                    content = str(paragraph).strip()
                    if content:
                        lines.append(f"- {content}")
        lines.append("同步结果：")
        lines.extend(sync_lines)
        return "\n".join(lines)

    def _default_doc_title(self, instruction: str, fallback: str | None = None) -> str:
        if fallback:
            return f"{settings.feishu_doc_title_prefix} - {fallback.strip()}"
        condensed = " ".join((instruction or "").split()).strip()
        if condensed:
            condensed = condensed[:24]
            return f"{settings.feishu_doc_title_prefix} - {condensed}"
        return f"{settings.feishu_doc_title_prefix} - 讨论整理"

    def _empty_result(self, session_id: str, mode: str) -> dict:
        return {
            "session_id": session_id,
            "mode": mode,
            "reply_preview": None,
            "reply_sent": False,
            "reply_error": None,
            "analysis": None,
        }

    def _deliver_reply(
        self,
        message: FeishuMessageContext,
        mode: str,
        reply_preview: str | None,
        *,
        analysis: AnalyzeResponse | None,
        episode_id: int | None = None,
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
            "episode_id": episode_id,
            "mode": mode,
            "analysis": analysis,
            "reply_preview": reply_preview,
            "reply_sent": reply_sent,
            "reply_error": reply_error,
        }

    def _should_close_episode(self, result: dict, reply_preview: str | None) -> bool:
        if not reply_preview:
            return True
        if not settings.feishu_reply_enabled:
            return True
        return bool(result.get("reply_sent"))

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
                lines.append("1. 当前没有识别到新的显性风险。")

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

        return [f"- 成功写入 {created} 条任务到多维表格。"] + failed

    def _format_status_reply(self, query: str, tasks: list, payload: dict) -> str:
        if not tasks:
            return "我这边还没有现成的任务快照。你可以先让我总结一下或整理待办，我再基于结果回答状态问题。"

        if "没负责人" in query or "未分配" in query:
            pending = [task for task in tasks if task.owner == "TBD"]
            if not pending:
                return "【负责人检查】\n当前任务都已经有明确负责人，没有未分配项。"
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
                "- 如需更具体输出，可以继续问：谁负责什么 / 哪些任务没负责人 / 当前有什么风险。",
            ]
        )

    def _format_presentation_reply(self, package: dict) -> str:
        theme = str(package.get("theme") or "基于群聊讨论的协作汇报").strip()
        audience = str(package.get("audience") or "项目汇报 / 路演准备").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        lines = ["【汇报大纲】", f"主题：{theme}", f"适用场景：{audience}"]

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

    def _build_fallback_presentation_package(self, session_id: str) -> dict:
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
                {"title": "讨论中形成的关键结论", "bullets": task_lines[:3]},
                {"title": "任务拆解与分工", "bullets": task_lines},
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
            "assets": ["最新任务清单截图", "关键讨论结论摘要", "时间线或里程碑信息"],
        }

    def _format_help_reply(self, reason: str | None = None) -> str:
        lines = ["【我可以这样帮你】"]
        if reason:
            lines.append(f"提示：{reason}")
        lines.extend(
            [
                "- @我 总结一下这次讨论",
                "- @我 帮我整理待办",
                "- @我 看一下当前风险和卡点",
                "- @我 现在还有哪些任务没负责人",
                "- @我 帮我把刚才讨论同步到多维表格",
                "- @我 帮我搞个汇报大纲",
            ]
        )
        return "\n".join(lines)
