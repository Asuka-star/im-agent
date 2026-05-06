from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.analyze import AgentTrace, AnalyzeResponse
from app.schemas.feishu_event import FeishuMessageContext
from app.schemas.feishu_card import FeishuCardActionEvent, FeishuCardActionPayload
from app.schemas.task import TaskItem
from app.services.cards.idempotency import CardActionDedupService
from app.services.due_date import normalize_task_dates
from app.services.text_analysis import build_next_actions, infer_risks, normalize_tasks
from app.services.tools.task_operation_tool import TaskOperationTool

logger = logging.getLogger(__name__)


class FeishuCardActionService:
    """Executes structured Feishu card button actions."""

    def __init__(
        self,
        workflow: Any,
        *,
        dedup_service: CardActionDedupService | None = None,
    ) -> None:
        self.workflow = workflow
        self.dedup_service = dedup_service or CardActionDedupService()

    def handle_raw_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.parse_event(payload)
        if event is None:
            logger.info("Ignored Feishu card callback without supported action payload")
            return {"code": 0, "msg": "ignored"}

        key = event.action.idempotency_key
        if not self.dedup_service.accept(key):
            previous = self.dedup_service.get_result(key) or {"duplicate": True}
            self._patch_card_status(
                event,
                title="选择已收到",
                content="这个按钮已经点过了，系统正在处理或已经处理完成，无需重复点击。",
                template="grey",
            )
            self._reply(event, "这个卡片操作已经处理过了，无需重复确认。")
            return {"code": 0, "msg": "duplicate_ignored", "data": previous}

        try:
            if self._confirmation_already_answered(event):
                result = {
                    "ok": True,
                    "action": event.action.action,
                    "duplicate": True,
                    "source": "confirmation_status",
                }
                self._patch_card_status(event, title="确认已处理", content="这个确认已经被处理过了，无需重复点击。")
                self._reply(event, "这个确认已经处理过了，无需重复点击。")
                self.dedup_service.finish(key, result)
                return {"code": 0, "msg": "duplicate_ignored", "data": result}
            result = self._dispatch(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Feishu card action failed: action=%s", event.action.action)
            result = {"ok": False, "error": str(exc), "action": event.action.action}
            self._patch_card_status(
                event,
                title="卡片操作失败",
                content=f"这次卡片操作没有执行成功：{exc}",
                template="red",
            )
            self._reply(event, f"这次卡片操作没有执行成功：{exc}")
        self.dedup_service.finish(key, result)
        return {"code": 0, "msg": "handled", "data": result}

    def parse_event(self, payload: dict[str, Any]) -> FeishuCardActionEvent | None:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        action_payload = self._extract_action_payload(payload)
        if not action_payload:
            return None
        try:
            action = FeishuCardActionPayload.model_validate(action_payload)
        except ValidationError as exc:
            logger.warning("Invalid Feishu card action payload: %s", exc)
            return None

        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        top_operator = payload.get("operator") if isinstance(payload.get("operator"), dict) else {}
        operator_id = self._first_non_empty(
            operator.get("user_id"),
            operator.get("open_id"),
            operator.get("union_id"),
            _nested(event, ("operator", "operator_id", "user_id")),
            _nested(event, ("operator", "operator_id", "open_id")),
            _nested(event, ("operator", "operator_id", "union_id")),
            top_operator.get("user_id"),
            top_operator.get("open_id"),
            top_operator.get("union_id"),
            _nested(payload, ("operator", "operator_id", "user_id")),
            _nested(payload, ("operator", "operator_id", "open_id")),
            _nested(payload, ("operator", "operator_id", "union_id")),
            payload.get("user_id"),
            payload.get("open_id"),
            payload.get("union_id"),
        )
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        return FeishuCardActionEvent(
            event_id=str(header.get("event_id") or payload.get("uuid") or "") or None,
            event_type=str(header.get("event_type") or payload.get("type") or "") or None,
            message_id=self._first_non_empty(
                event.get("message_id"),
                message.get("message_id"),
                context.get("open_message_id"),
                payload.get("message_id"),
                payload.get("open_message_id"),
            ),
            chat_id=self._first_non_empty(
                event.get("chat_id"),
                message.get("chat_id"),
                context.get("open_chat_id"),
                payload.get("chat_id"),
                payload.get("open_chat_id"),
                action.payload.get("chat_id"),
                action.session_id,
            ),
            operator_id=operator_id,
            action=action,
            raw_payload=payload,
        )

    def _dispatch(self, event: FeishuCardActionEvent) -> dict[str, Any]:
        action = event.action.action
        if action == "confirm_task_status":
            return self._confirm_task_status(event)
        if action == "cancel_task_update":
            return self._cancel_task_update(event)
        if action == "create_delivery_bundle":
            return self._create_delivery_bundle(event)
        if action == "select_clarification_option":
            return self._select_clarification_option(event)
        raise ValueError(f"不支持的卡片动作：{action}")

    def _confirm_task_status(self, event: FeishuCardActionEvent) -> dict[str, Any]:
        action = event.action
        session_id = action.session_id
        if not session_id:
            raise ValueError("缺少 session_id")
        target_status = str(action.payload.get("target_status") or "done").strip().lower()
        if target_status not in {"done", "cancelled", "canceled"}:
            raise ValueError(f"不支持的任务状态：{target_status}")
        target_payload = action.payload.get("task") if isinstance(action.payload.get("task"), dict) else {}
        target_title = str(target_payload.get("title") or "").strip()
        target_owner = str(target_payload.get("owner") or "").strip()
        if not target_title:
            raise ValueError("缺少任务标题")

        current_tasks = self._current_tasks_for_session(session_id, chat_id=event.chat_id)
        target_index = self._find_task_index(current_tasks, title=target_title, owner=target_owner)
        if target_index is None:
            raise ValueError("没有找到对应任务，请在 Workbench 中确认")
        confirmation = self._resolve_confirmation_if_needed(
            event,
            answer_value=f"{target_owner} - {target_title} -> {target_status}",
        )
        if getattr(confirmation, "already_answered", False):
            self._patch_card_status(event, title="确认已处理", content="这个确认已经被处理过了，无需重复点击。")
            self._reply(event, "这个确认已经处理过了，无需重复点击。")
            return {
                "ok": True,
                "action": action.action,
                "duplicate": True,
                "source": "confirmation_status",
            }

        tasks = [task.model_copy(deep=True) for task in current_tasks]
        target = tasks[target_index]
        note = f"飞书卡片确认更新：{event.operator_id or 'unknown'}"
        notes = target.notes or ""
        if note not in notes:
            notes = f"{notes}；{note}".strip("；")
        tasks[target_index] = target.model_copy(update={"status": target_status, "notes": notes})
        tasks = normalize_task_dates(normalize_tasks(tasks))
        updated_task = tasks[target_index]
        analysis = self._build_task_status_update_analysis(
            session_id,
            tasks,
            source_text=f"card:{action.source_message_id or action.idempotency_key}",
            updated_task=updated_task,
        )
        self.workflow.memory_service.save_round(
            session_id=session_id,
            analysis=analysis,
            episode_id=None,
            source_message_id=action.source_message_id or action.idempotency_key,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        if action.task_run_id:
            self.workflow.task_run_service.upsert_step(
                action.task_run_id,
                step_key="card_confirm_task_status",
                title="飞书卡片确认任务状态",
                step_type="confirmation",
                status="done",
                output_payload={
                    "title": target_title,
                    "owner": target_owner,
                    "status": target_status,
                    "operator_id": event.operator_id,
                },
            )
            self.workflow.task_run_service.update_task_run(
                action.task_run_id,
                status="completed",
                stage="delivered",
                latest_summary=analysis.summary,
                latest_reply_preview=self.workflow.response_formatter.format_task_status_update_reply(
                    updated_task,
                    target_status,
                ),
            )
        reply = self.workflow.response_formatter.format_task_status_update_reply(updated_task, target_status)
        self._patch_card_status(
            event,
            title="任务状态已更新",
            content=f"已将「{target_title}」更新为 {target_status}。",
        )
        self._reply(event, reply)
        return {
            "ok": True,
            "action": action.action,
            "session_id": session_id,
            "task": updated_task.model_dump(),
            "status": target_status,
        }

    def _cancel_task_update(self, event: FeishuCardActionEvent) -> dict[str, Any]:
        confirmation = self._resolve_confirmation_if_needed(event, answer_value="cancelled")
        if getattr(confirmation, "already_answered", False):
            self._patch_card_status(event, title="确认已处理", content="这个确认已经被处理过了，无需重复点击。")
            self._reply(event, "这个确认已经处理过了，无需重复点击。")
            return {"ok": True, "action": event.action.action, "duplicate": True}
        if event.action.task_run_id:
            self.workflow.task_run_service.update_task_run(
                event.action.task_run_id,
                status="completed",
                stage="confirmation_cancelled",
                latest_summary="用户取消了飞书卡片任务更新。",
            )
        self._patch_card_status(event, title="已取消", content="本次任务更新已取消，未写入任务状态。", template="grey")
        self._reply(event, "已取消本次任务更新，没有写入任务状态。")
        return {"ok": True, "action": event.action.action, "cancelled": True}

    def _create_delivery_bundle(self, event: FeishuCardActionEvent) -> dict[str, Any]:
        task_run_id = event.action.task_run_id
        if not task_run_id:
            raise ValueError("缺少 task_run_id，无法生成交付包")
        self._patch_card_status(event, title="交付包生成中", content="已开始整理交付包，完成后会在任务详情和 IM 中展示。")
        detail = self.workflow.bundle_delivery_from_task_run(
            task_run_id,
            requested_by=event.operator_id or "feishu_card",
        )
        if detail is None:
            raise ValueError("没有找到对应 task run，无法生成交付包")
        self._patch_card_status(event, title="交付包已生成", content="交付包已整理完成，新的交付包卡片会回发到 IM。")
        self._reply(event, "已开始整理交付包，完成后会在任务详情和 IM 中展示。")
        return {
            "ok": True,
            "action": event.action.action,
            "task_run_id": task_run_id,
            "result_task_run_id": getattr(detail, "task_run_id", task_run_id),
        }

    def _select_clarification_option(self, event: FeishuCardActionEvent) -> dict[str, Any]:
        option = str(event.action.payload.get("option") or "").strip()
        display_option = option or "未命名选项"
        self._patch_card_status(
            event,
            title="已收到选择",
            content=f"已选择：{display_option}\n\n系统正在继续处理，请不要重复点击。完成后我会在群里回复结果。",
            template="blue",
        )
        self._reply(event, f"已收到你的选择：{display_option}。我正在继续处理，完成后会把结果发到这里。")
        confirmation = self._resolve_confirmation_if_needed(event, answer_value=option)
        if getattr(confirmation, "already_answered", False):
            self._patch_card_status(event, title="确认已处理", content="这个确认已经被处理过了，无需重复点击。")
            self._reply(event, "这个确认已经处理过了，无需重复点击。")
            return {"ok": True, "action": event.action.action, "option": option, "duplicate": True, "resumed": False}
        resumed = self._resume_after_confirmation(event, answer_value=option)
        reply = None
        if isinstance(resumed, dict):
            reply = str(resumed.get("reply_preview") or "").strip() or None
        resumed_ok = bool(resumed)
        if resumed_ok:
            status_content = f"已选择：{option or '未命名选项'}。系统已继续处理后续流程。"
        else:
            status_content = f"已选择：{option or '未命名选项'}，但后续流程没有自动继续，请在 Workbench 中重试或重新发起请求。"
            if event.action.task_run_id:
                try:
                    self.workflow.task_run_service.update_task_run(
                        event.action.task_run_id,
                        stage="confirmation_resume_failed",
                        status="failed",
                        latest_error="Card confirmation was recorded but resume did not produce a result.",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to mark task run resume failure from card action: %s", exc)
        self._patch_card_status(
            event,
            title="确认已收到",
            content=status_content,
        )
        self._reply(event, reply or status_content)
        return {"ok": True, "action": event.action.action, "option": option, "resumed": resumed_ok}

    def _current_tasks_for_session(self, session_id: str, *, chat_id: str | None = None) -> list[TaskItem]:
        pseudo_message = FeishuMessageContext(
            message_id=None,
            chat_id=chat_id or session_id,
            chat_type="group",
            message_type="interactive",
            session_id=session_id,
            sender_id="feishu_card",
            text="",
            raw_text="",
        )
        try:
            _, _, base_tasks = self.workflow._base_status_task_sources_for_message(pseudo_message)
            if base_tasks:
                return [
                    task if isinstance(task, TaskItem) else TaskItem.model_validate(task)
                    for task in base_tasks
                ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load base tasks for card action, falling back to memory: %s", exc)
        rows = self.workflow.memory_service.get_current_tasks(session_id)
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

    def _find_task_index(self, tasks: list[TaskItem], *, title: str, owner: str) -> int | None:
        target = TaskItem(title=title, owner=owner or "TBD")
        index = TaskOperationTool.find_merge_target(tasks, target)
        if index is not None:
            return index
        title_matches = [
            idx
            for idx, task in enumerate(tasks)
            if _normalized(task.title) == _normalized(title)
            and str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
        ]
        if owner:
            owner_matches = [idx for idx in title_matches if _normalized(tasks[idx].owner) == _normalized(owner)]
            if len(owner_matches) == 1:
                return owner_matches[0]
        if len(title_matches) == 1:
            return title_matches[0]
        return None

    def _build_task_status_update_analysis(
        self,
        session_id: str,
        tasks: list[TaskItem],
        *,
        source_text: str,
        updated_task: TaskItem,
    ) -> AnalyzeResponse:
        risks = infer_risks(tasks)
        updated_label = f"{updated_task.owner} - {updated_task.title}"
        return AnalyzeResponse(
            session_id=session_id,
            summary=f"已根据飞书卡片确认更新任务状态：{updated_label}。",
            tasks=tasks,
            risks=risks,
            next_actions=build_next_actions(tasks, risks),
            agent_traces=[
                AgentTrace(agent="card_action", summary=f"处理飞书卡片任务确认：{source_text}"),
            ],
        )

    def _resolve_confirmation_if_needed(self, event: FeishuCardActionEvent, *, answer_value: str) -> Any:
        task_run_id = event.action.task_run_id
        confirmation_id = str(event.action.payload.get("confirmation_id") or "").strip()
        if not task_run_id or not confirmation_id:
            return None
        try:
            return self.workflow.task_run_service.resolve_confirmation(
                task_run_id,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=event.operator_id or "feishu_card",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve confirmation from card action: %s", exc)
            return None

    def _resume_after_confirmation(self, event: FeishuCardActionEvent, *, answer_value: str) -> dict | None:
        task_run_id = event.action.task_run_id
        confirmation_id = str(event.action.payload.get("confirmation_id") or "").strip()
        if not task_run_id or not confirmation_id:
            return None
        resume = getattr(self.workflow, "resume_task_run_after_confirmation", None)
        if not callable(resume):
            return None
        try:
            result = resume(
                task_run_id,
                confirmation_id=confirmation_id,
                answer_value=answer_value,
                answered_by=event.operator_id or "feishu_card",
            )
            return result if isinstance(result, dict) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resume task run from card action: %s", exc)
            return None

    def _confirmation_already_answered(self, event: FeishuCardActionEvent) -> bool:
        task_run_id = event.action.task_run_id
        confirmation_id = str(event.action.payload.get("confirmation_id") or "").strip()
        if not task_run_id or not confirmation_id:
            return False
        get_task_run = getattr(self.workflow.task_run_service, "get_task_run", None)
        if not callable(get_task_run):
            return False
        try:
            detail = get_task_run(task_run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to inspect confirmation status for card action: %s", exc)
            return False
        confirmations = getattr(detail, "confirmations", []) if detail is not None else []
        for item in confirmations or []:
            if isinstance(item, dict):
                item_id = str(item.get("confirmation_id") or "").strip()
                status = str(item.get("status") or "").strip().lower()
            else:
                item_id = str(getattr(item, "confirmation_id", "") or "").strip()
                status = str(getattr(item, "status", "") or "").strip().lower()
            if item_id == confirmation_id and status == "answered":
                return True
        return False

    def _reply(self, event: FeishuCardActionEvent, text: str) -> None:
        chat_id = event.chat_id or event.action.payload.get("chat_id") or event.action.session_id
        if not chat_id or not settings.feishu_reply_enabled:
            return
        try:
            self.workflow.message_api.send_text_message(str(chat_id), text, receive_id_type="chat_id")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to reply to Feishu card action: %s", exc)

    def _patch_card_status(
        self,
        event: FeishuCardActionEvent,
        *,
        title: str,
        content: str,
        template: str = "green",
    ) -> None:
        if not event.message_id:
            return
        patch_card = getattr(self.workflow.message_api, "patch_card", None)
        if not callable(patch_card):
            return
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            ],
        }
        try:
            patch_card(event.message_id, card)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to patch Feishu card after action: %s", exc)

    def _extract_action_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [
            _nested(payload, ("event", "action", "value")),
            _nested(payload, ("event", "action", "form_value")),
            _nested(payload, ("event", "action", "option")),
            _nested(payload, ("event", "value")),
            _nested(payload, ("action", "value")),
            payload.get("value"),
        ]
        for candidate in candidates:
            parsed = self._coerce_action_payload(candidate)
            if parsed:
                return parsed
        return None

    def _coerce_action_payload(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if "action" in value and "idempotency_key" in value:
                return value
            nested_value = value.get("value")
            if isinstance(nested_value, dict) and "action" in nested_value:
                return nested_value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        return None

    @staticmethod
    def _first_non_empty(*values: Any) -> str | None:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return None


def _nested(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalized(value: str | None) -> str:
    return " ".join((value or "").lower().split())
