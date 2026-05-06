from __future__ import annotations

import re

from app.schemas.task import TaskItem


def merge_status_task_sources(primary: list, secondary: list) -> list:
    merged: list[TaskItem] = []
    for task in [*(primary or []), *(secondary or [])]:
        title = _task_value(task, "title").strip()
        owner = _task_value(task, "owner").strip()
        if not title:
            continue
        normalized = _normalize_task_item(task, title=title, owner=owner)
        if normalized is None:
            continue
        target_index = _find_status_merge_target(merged, normalized)
        if target_index is not None:
            existing = merged[target_index]
            if _task_title_key(existing.title) == _task_title_key(normalized.title) and _normalized(existing.owner) == _normalized(normalized.owner):
                merged[target_index] = normalized
            else:
                merged[target_index] = _merge_status_task(existing, normalized)
            continue
        merged.append(normalized)
    return merged

def _normalize_task_item(task: object, *, title: str, owner: str) -> TaskItem | None:
    if isinstance(task, TaskItem):
        return task
    if isinstance(task, dict):
        try:
            return TaskItem.model_validate(task)
        except Exception:
            return None
    return TaskItem(
        title=title,
        owner=owner or "TBD",
        priority=_task_value(task, "priority", default="medium") or "medium",
        due_date=_task_value(task, "due_date", default="TBD") or "TBD",
        status=_task_value(task, "status", default="draft") or "draft",
        notes=_task_value(task, "notes"),
    )

def _find_status_merge_target(tasks: list[TaskItem], incoming: TaskItem) -> int | None:
    incoming_title = _task_title_key(incoming.title)
    incoming_owner = _normalized(incoming.owner)
    exact_matches = [
        index
        for index, task in enumerate(tasks)
        if _task_title_key(task.title) == incoming_title and _normalized(task.owner) == incoming_owner
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    title_matches = [
        index
        for index, task in enumerate(tasks)
        if _task_title_key(task.title) == incoming_title
    ]
    if len(title_matches) == 1:
        existing_owner = _normalized(tasks[title_matches[0]].owner)
        if _is_placeholder_owner(existing_owner) or _is_placeholder_owner(incoming_owner):
            return title_matches[0]
        return title_matches[0]
    return None

def _merge_status_task(existing: TaskItem, incoming: TaskItem) -> TaskItem:
    return existing.model_copy(
        update={
            "owner": _merge_owner_labels(existing.owner, incoming.owner),
            "priority": _pick_higher_priority(existing.priority, incoming.priority),
            "due_date": _pick_due_date(existing.due_date, incoming.due_date),
            "status": _pick_status(existing.status, incoming.status),
            "notes": _merge_notes(existing.notes, incoming.notes),
        }
    )

def _merge_owner_labels(existing: str | None, incoming: str | None) -> str:
    owners: list[str] = []
    for raw in (existing, incoming):
        for part in re.split(r"\s*(?:、|,|，|/|;|；)\s*", str(raw or "")):
            owner = part.strip()
            if not owner or _is_placeholder_owner(_normalized(owner)) or owner in owners:
                continue
            owners.append(owner)
    return "、".join(owners) if owners else "TBD"

def _pick_higher_priority(existing: str | None, incoming: str | None) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    existing_value = order.get(_normalized(existing), 1)
    incoming_value = order.get(_normalized(incoming), 1)
    return incoming or "medium" if incoming_value > existing_value else existing or "medium"

def _pick_due_date(existing: str | None, incoming: str | None) -> str:
    existing_value = str(existing or "").strip()
    incoming_value = str(incoming or "").strip()
    if not incoming_value or _is_placeholder_due(incoming_value):
        return existing_value or "TBD"
    if not existing_value or _is_placeholder_due(existing_value):
        return incoming_value
    return incoming_value

def _pick_status(existing: str | None, incoming: str | None) -> str:
    incoming_value = str(incoming or "").strip()
    return incoming_value or str(existing or "").strip() or "draft"

def _merge_notes(existing: str | None, incoming: str | None) -> str:
    notes: list[str] = []
    for value in (existing, incoming):
        note = str(value or "").strip()
        if note and note not in notes:
            notes.append(note)
    return "；".join(notes)

def _task_value(task: object, field: str, *, default: str = "") -> str:
    if isinstance(task, dict):
        return str(task.get(field) or default)
    return str(getattr(task, field, default) or default)

def _normalized(value: str | None) -> str:
    return " ".join(str(value or "").lower().split())

def _task_title_key(value: str | None) -> str:
    normalized = _normalized(value)
    compact = normalized.replace(" ", "")
    if not compact:
        return ""
    generic_ppt = _generic_artifact_title_key(
        compact,
        artifact_tokens=("ppt", "powerpoint"),
        generic_tokens=(
            "制作",
            "生成",
            "做",
            "准备",
            "整理",
            "输出",
            "产出",
            "创建",
            "汇报",
            "演示",
            "大纲",
            "材料",
            "幻灯片",
            "演示稿",
            "generation",
            "generate",
            "create",
            "make",
            "build",
            "outline",
            "slides",
            "slide",
            "deck",
            "presentation",
        ),
    )
    if generic_ppt:
        return generic_ppt
    generic_canvas = _generic_artifact_title_key(
        compact,
        artifact_tokens=("canvas", "画布"),
        generic_tokens=("制作", "生成", "做", "准备", "整理", "输出", "产出", "创建", "绘制", "画", "流程图"),
    )
    if generic_canvas:
        return generic_canvas
    return compact

def _generic_artifact_title_key(compact: str, *, artifact_tokens: tuple[str, ...], generic_tokens: tuple[str, ...]) -> str:
    matched_token = next((token for token in artifact_tokens if token in compact), "")
    if not matched_token:
        return ""
    remaining = compact
    for token in (*artifact_tokens, *generic_tokens):
        remaining = remaining.replace(token, "")
    return matched_token if not remaining else ""

def _is_placeholder_owner(value: str) -> bool:
    return value in {"", "tbd", "待定", "未定", "待确认", "待確認", "unassigned"}

def _is_placeholder_due(value: str) -> bool:
    return _normalized(value) in {"", "tbd", "待定", "未定", "待确认", "待確認"}

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
