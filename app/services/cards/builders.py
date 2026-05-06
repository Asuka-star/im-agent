from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.task import TaskItem
from app.services.cards.action_payloads import build_card_action_payload


class FeishuCardBuilder:
    """Build Feishu interactive cards used by the IM workflow."""

    def build_artifact_delivery_card(
        self,
        *,
        title: str,
        mode: str,
        artifacts: list[dict] | None,
        summary: str | None = None,
        task_run_id: str | None = None,
        source_message_id: str | None = None,
        session_id: str | None = None,
        checks: list[dict] | None = None,
    ) -> dict | None:
        artifact_items = self._artifact_items(artifacts or [])
        if not artifact_items:
            return None
        ready_count = sum(1 for item in artifact_items if item["status"] == "ready")
        lines = [
            f"**处理类型**：{self._mode_label(mode)}",
            f"**协作产物**：{len(artifact_items)} 个，{ready_count} 个已就绪",
        ]
        if summary:
            lines.append(f"**摘要**：{self._shorten(summary, 100)}")
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
        ]
        for item in artifact_items[:4]:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{item['label']}**：{item['title']}\n{item['detail']}",
                    },
                }
            )
        check_lines = self._check_lines(checks or [])
        if check_lines:
            elements.extend(
                [
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(check_lines)}},
                ]
            )

        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": item["button_text"]},
                "url": item["url"],
                "type": "primary" if index == 0 else "default",
            }
            for index, item in enumerate(artifact_items[:3])
            if item.get("url")
        ]
        if actions:
            elements.append({"tag": "action", "actions": actions[:4]})
        return self._card(title=title or "AI 协作产物已生成", template="turquoise", elements=elements)

    def build_task_confirmation_card(
        self,
        *,
        session_id: str,
        task_run_id: str | None,
        source_message_id: str | None,
        question: str,
        reason: str | None,
        candidates: list[TaskItem | dict],
        target_status: str,
        confirmation_id: str | None = None,
    ) -> dict | None:
        normalized = [self._task_candidate(item) for item in candidates if self._task_candidate(item)]
        if not normalized:
            return None
        lines = [f"**需要确认**：{question}"]
        if reason:
            lines.append(f"**原因**：{self._shorten(reason, 120)}")
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
        ]
        for index, task in enumerate(normalized[:5], start=1):
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**候选 {index}**：{task['owner']} - {task['title']}\n"
                            f"当前状态：{task['status']}｜截止：{task['due_date']}"
                        ),
                    },
                }
            )
        actions: list[dict[str, Any]] = []
        for index, task in enumerate(normalized[:3], start=1):
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"确认候选 {index}"},
                    "type": "primary" if index == 1 else "default",
                    "value": build_card_action_payload(
                        "confirm_task_status",
                        session_id=session_id,
                        task_run_id=task_run_id,
                        source_message_id=source_message_id,
                        payload={
                            "task": task,
                            "target_status": target_status,
                            "confirmation_id": confirmation_id,
                        },
                    ),
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消"},
                "type": "default",
                "value": build_card_action_payload(
                    "cancel_task_update",
                    session_id=session_id,
                    task_run_id=task_run_id,
                    source_message_id=source_message_id,
                    payload={"confirmation_id": confirmation_id},
                ),
            }
        )
        elements.append({"tag": "action", "actions": actions[:4]})
        return self._card(title="需要确认任务更新", template="orange", elements=elements)

    def build_clarification_card(
        self,
        *,
        session_id: str,
        task_run_id: str | None,
        source_message_id: str | None,
        question: str,
        reason: str | None,
        options: list[str],
        confirmation_id: str | None = None,
    ) -> dict | None:
        cleaned = [str(item).strip() for item in options if str(item).strip()]
        if not cleaned:
            return None
        lines = [f"**需要确认**：{question}"]
        if reason:
            lines.append(f"**原因**：{self._shorten(reason, 120)}")
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        ]
        option_lines = [
            self._option_line(option_index + 1, cleaned[option_index])
            for option_index in self._clarification_display_indexes(len(cleaned))
        ]
        if option_lines:
            elements.extend(
                [
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(option_lines)}},
                ]
            )
        action_indexes = self._clarification_action_indexes(cleaned)
        actions = []
        for action_index, option_index in enumerate(action_indexes):
            option = cleaned[option_index]
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": self._shorten(option, 18)},
                    "type": "primary" if action_index == 0 else "default",
                    "value": build_card_action_payload(
                        "select_clarification_option",
                        session_id=session_id,
                        task_run_id=task_run_id,
                        source_message_id=source_message_id,
                        payload={
                            "option": option,
                            "option_index": option_index,
                            "confirmation_id": confirmation_id,
                        },
                    ),
                }
            )
        elements.append({"tag": "action", "actions": actions})
        return self._card(title="需要你确认一下", template="orange", elements=elements)

    def _card(self, *, title: str, template: str, elements: list[dict[str, Any]]) -> dict:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": self._shorten(title, 48)},
            },
            "elements": elements,
        }

    def _artifact_items(self, artifacts: list[dict]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            artifact_type = str(artifact.get("artifact_type") or "artifact")
            url = self._absolute_url(str(artifact.get("url") or "").strip())
            exports = self._exports(artifact.get("preview"))
            if not url and exports:
                url = exports[0]["url"]
            if not url:
                continue
            label = self._artifact_label(artifact_type)
            result.append(
                {
                    "artifact_type": artifact_type,
                    "label": label,
                    "title": self._shorten(str(artifact.get("title") or label), 42),
                    "status": str(artifact.get("status") or "ready"),
                    "url": url,
                    "button_text": self._button_text(artifact_type),
                    "detail": self._detail(artifact, exports),
                }
            )
        return result

    def _exports(self, preview: object) -> list[dict[str, str]]:
        if not isinstance(preview, dict):
            return []
        exports = preview.get("exports")
        if not isinstance(exports, dict):
            return []
        result = []
        for key, value in exports.items():
            url = self._absolute_url(str(value or "").strip())
            if url:
                result.append({"label": str(key).upper(), "url": url})
        return result

    def _detail(self, artifact: dict, exports: list[dict[str, str]]) -> str:
        artifact_type = str(artifact.get("artifact_type") or "")
        preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
        if artifact_type == "slides_package":
            slides = preview.get("slides") if isinstance(preview.get("slides"), list) else []
            notes = sum(
                1
                for item in slides
                if isinstance(item, dict) and str(item.get("speaker_notes") or "").strip()
            )
            return f"{len(slides)} 页，讲者备注 {notes}/{len(slides)} 页"
        if artifact_type == "canvas":
            summary = preview.get("summary") if isinstance(preview.get("summary"), dict) else {}
            node_count = str(summary.get("node_count") or "")
            arrow_count = str(summary.get("arrow_count") or "")
            if node_count or arrow_count:
                return f"{node_count or 0} 节点，{arrow_count or 0} 连线"
        if exports:
            return "导出：" + "、".join(item["label"] for item in exports[:3])
        return "可在链接中查看"

    def _check_lines(self, checks: list[dict]) -> list[str]:
        visible = [
            item
            for item in checks
            if isinstance(item, dict)
            and item.get("key") in {"document", "canvas", "slides", "rehearsal", "delivery_bundle"}
        ]
        if not visible:
            return []
        ready = sum(1 for item in visible if item.get("status") == "ready")
        lines = [f"**验收摘要**：{ready}/{len(visible)} 项已满足"]
        for item in visible[:5]:
            label = str(item.get("label") or item.get("key") or "检查项")
            status = self._check_status_label(str(item.get("status") or "missing"))
            detail = str(item.get("detail") or "").strip()
            lines.append(f"- {label}：{status}" + (f"（{detail}）" if detail else ""))
        return lines

    def _absolute_url(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        base = str(settings.artifact_public_base_url or "").rstrip("/")
        if not base:
            return value
        return f"{base}/{value.lstrip('/')}"

    def _artifact_label(self, artifact_type: str) -> str:
        labels = {
            "document": "协作文档",
            "doc": "协作文档",
            "feishu_doc": "飞书文档",
            "slides": "演示稿",
            "slides_package": "演示稿",
            "canvas": "白板 / Canvas",
            "delivery_bundle": "交付包",
        }
        return labels.get(artifact_type, "协作产物")

    def _button_text(self, artifact_type: str) -> str:
        if artifact_type == "delivery_bundle":
            return "打开交付包"
        if artifact_type in {"slides", "slides_package"}:
            return "打开演示稿"
        if artifact_type == "canvas":
            return "打开白板"
        if artifact_type in {"document", "doc", "feishu_doc"}:
            return "打开文档"
        return "打开产物"

    def _mode_label(self, mode: str) -> str:
        labels = {
            "doc": "文档",
            "slides": "演示稿",
            "canvas": "白板",
            "delivery": "交付",
            "analysis": "分析",
            "tasks": "任务",
        }
        return labels.get(mode, mode or "协作")

    def _check_status_label(self, status: str) -> str:
        if status == "ready":
            return "已满足"
        if status == "partial":
            return "部分满足"
        if status == "missing":
            return "待补齐"
        return status or "未知"

    def _task_candidate(self, item: TaskItem | dict) -> dict[str, str]:
        if isinstance(item, TaskItem):
            data = item.model_dump()
        elif isinstance(item, dict):
            data = item
        else:
            return {}
        title = str(data.get("title") or "").strip()
        if not title:
            return {}
        return {
            "title": title,
            "owner": str(data.get("owner") or "TBD").strip() or "TBD",
            "priority": str(data.get("priority") or "medium").strip() or "medium",
            "due_date": str(data.get("due_date") or "TBD").strip() or "TBD",
            "status": str(data.get("status") or "draft").strip() or "draft",
            "notes": str(data.get("notes") or "").strip(),
        }

    def _shorten(self, value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(limit - 1, 1)].rstrip() + "…"

    def _option_line(self, index: int, option: str) -> str:
        text = self._shorten(option, 72)
        if self._has_visible_index(text):
            return f"- {text}"
        return f"- {index}. {text}"

    @staticmethod
    def _clarification_action_indexes(options: list[str]) -> list[int]:
        option_count = len(options)
        if option_count <= 3:
            return list(range(option_count))
        if _looks_like_other_option(options[-1]):
            return [0, 1, option_count - 1]
        return [0, 1, 2]

    @staticmethod
    def _clarification_display_indexes(option_count: int) -> list[int]:
        if option_count <= 5:
            return list(range(option_count))
        return [0, 1, 2, 3, option_count - 1]

    @staticmethod
    def _has_visible_index(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        first = text.split(maxsplit=1)[0]
        return first.rstrip(".、)）").isdigit()


def _looks_like_other_option(value: str) -> bool:
    text = str(value or "").strip()
    return any(marker in text for marker in ("其他", "新建", "新需求", "另一个需求"))
