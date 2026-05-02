from __future__ import annotations

import re

from app.schemas.task import TaskItem


def merge_status_task_sources(primary: list, secondary: list) -> list:
    merged: list[TaskItem] = []
    seen: set[tuple[str, str]] = set()
    for task in [*(primary or []), *(secondary or [])]:
        title = str(getattr(task, "title", "") if not isinstance(task, dict) else task.get("title") or "").strip()
        owner = str(getattr(task, "owner", "") if not isinstance(task, dict) else task.get("owner") or "").strip()
        if not title:
            continue
        key = (title.lower(), owner.lower())
        if key in seen:
            continue
        seen.add(key)
        if isinstance(task, TaskItem):
            merged.append(task)
        elif isinstance(task, dict):
            try:
                merged.append(TaskItem.model_validate(task))
            except Exception:
                continue
        else:
            merged.append(
                TaskItem(
                    title=title,
                    owner=owner or "TBD",
                    priority=str(getattr(task, "priority", "medium") or "medium"),
                    due_date=str(getattr(task, "due_date", "TBD") or "TBD"),
                    status=str(getattr(task, "status", "draft") or "draft"),
                    notes=str(getattr(task, "notes", "") or ""),
                )
            )
    return merged

def task_items_from_llm_payload(payload: dict) -> list[TaskItem]:
    raw_tasks = payload.get("tasks") if isinstance(payload, dict) else []
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[TaskItem] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            continue
        try:
            tasks.append(TaskItem.model_validate(item))
        except Exception:
            continue
    return tasks

def tasks_from_document_snapshot(snapshot: list[dict]) -> list[TaskItem]:
    tasks: list[TaskItem] = []
    seen: set[tuple[str, str, str]] = set()
    task_heading_markers = ("任务", "待办", "分工", "计划", "事项", "todo", "task")
    for section in snapshot:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "").strip()
        paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
        in_task_section = any(marker in heading.lower() for marker in task_heading_markers)
        pending_title = ""
        for paragraph in paragraphs:
            for raw_line in str(paragraph or "").splitlines():
                line = clean_document_task_line(raw_line)
                if not line:
                    continue
                item = task_item_from_document_line(line, fallback_title=pending_title)
                if item is None and in_task_section and looks_like_task_title(line):
                    pending_title = line
                    continue
                if item is None:
                    pending_title = ""
                    continue
                key = (item.title.strip().lower(), item.owner.strip().lower(), item.due_date.strip().lower())
                if item.title and key not in seen:
                    seen.add(key)
                    tasks.append(item)
                pending_title = ""
    return tasks

def clean_document_task_line(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s+", "", text)
    text = re.sub(r"^\s*\d+[.)、]\s*", "", text)
    text = text.strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text[2:-2].strip()
    return text

def looks_like_task_title(line: str) -> bool:
    text = str(line or "").strip()
    if not text or len(text) > 40:
        return False
    if any(marker in text for marker in ("负责人", "截止", "到期", "优先级", "状态", "|")):
        return False
    if text.endswith(("。", "，", "；", ".", ",")):
        return False
    return True

def task_item_from_document_line(line: str, *, fallback_title: str = "") -> TaskItem | None:
    text = str(line or "").strip()
    if not text:
        return None
    if "|" in text:
        return task_item_from_pipe_row(text)
    owner = field_value_from_text(text, ("负责人", "owner"))
    due_date = field_value_from_text(text, ("截止", "到期", "due"))
    priority = field_value_from_text(text, ("优先级", "priority")) or "medium"
    status = field_value_from_text(text, ("状态", "status")) or "draft"
    if not owner:
        return None
    title = fallback_title or title_before_first_field(text)
    if not title:
        return None
    return TaskItem(title=title, owner=owner, due_date=due_date or "TBD", priority=priority, status=status)

def task_item_from_pipe_row(line: str) -> TaskItem | None:
    parts = [part.strip().strip("-") for part in str(line or "").split("|") if part.strip()]
    if not parts:
        return None
    content_title = ""
    speaker_note = ""
    for part in parts:
        if is_speaker_metadata(part):
            speaker_note = part
            continue
        content_title = loose_labeled_value(part, ("内容", "任务内容", "任务描述", "描述", "需求"))
        if content_title:
            break
    first_part_is_metadata = is_speaker_metadata(parts[0])
    title = content_title if content_title else strip_inline_label(parts[0], ("任务", "事项", "标题"))
    if first_part_is_metadata and not content_title:
        title = ""
    owner = ""
    due_date = "TBD"
    priority = "medium"
    status = "draft"
    notes: list[str] = []
    for index, part in enumerate(parts[1:], start=1):
        labeled_owner = field_value_from_text(part, ("负责人", "owner"))
        labeled_due = field_value_from_text(part, ("截止", "到期", "due"))
        labeled_priority = field_value_from_text(part, ("优先级", "priority"))
        labeled_status = field_value_from_text(part, ("状态", "status"))
        if labeled_owner:
            owner = labeled_owner
        elif labeled_due:
            due_date = labeled_due
        elif labeled_priority:
            priority = labeled_priority
        elif labeled_status:
            status = labeled_status
        elif index == 1 and not owner and not any(marker in part for marker in ("截止", "到期", "优先级", "状态")):
            if not is_speaker_metadata(part) and not loose_labeled_value(
                part, ("内容", "任务内容", "任务描述", "描述", "需求")
            ):
                owner = part
        else:
            if not loose_labeled_value(part, ("内容", "任务内容", "任务描述", "描述", "需求")):
                notes.append(part)
    if not title:
        return None
    if speaker_note:
        notes.append(speaker_note)
    return TaskItem(
        title=title,
        owner=owner or "TBD",
        due_date=due_date or "TBD",
        priority=priority or "medium",
        status=status or "draft",
        notes="；".join(notes),
    )

def is_speaker_metadata(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(re.match(r"^(发言人|发言者|用户|发送人|sender|speaker)\s*[:：]?\s*\S+", value, flags=re.IGNORECASE))

def loose_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    value = str(text or "").strip()
    for label in labels:
        match = re.match(rf"^{re.escape(label)}\s*[:：]?\s*(.+)$", value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""

def strip_inline_label(text: str, labels: tuple[str, ...]) -> str:
    value = str(text or "").strip()
    for label in labels:
        match = re.match(rf"^{re.escape(label)}\s*[:：]\s*(.+)$", value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return value

def field_value_from_text(text: str, labels: tuple[str, ...]) -> str:
    value = str(text or "").strip()
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*[:：]\s*([^|，,；;\n]+)",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
    return ""

def title_before_first_field(text: str) -> str:
    value = str(text or "").strip()
    match = re.split(r"\s*(?:负责人|owner|截止|到期|due|优先级|priority|状态|status)\s*[:：]", value, maxsplit=1)
    return match[0].strip(" -:：|") if match else ""
