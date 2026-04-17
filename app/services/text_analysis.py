import re
from typing import Iterable

from app.schemas.task import TaskItem


SENDER_PREFIX_RE = re.compile(r"^\s*[-*]?\s*[A-Za-z0-9_]{4,}:\s*")
LEADING_FILLER_RE = re.compile(r"^(不对|另外|然后|还有|补充一个|补充|顺便|以及|目前|近期群聊讨论)\s*[，,:：]?\s*")
DIALOG_OWNER_RE = re.compile(r"^(?P<owner>[\u4e00-\u9fa5A-Za-z0-9]{1,8})(?=你|同学|老师|这边|这个)")
ACTION_OWNER_RE = re.compile(
    r"^(?P<owner>[\u4e00-\u9fa5A-Za-z0-9]{1,8})(?=负责|完成|准备|处理|跟进|推进|搞|做)"
)
DUE_HINT_RE = re.compile(
    r"((?:本周|这周|下周)?(?:周[一二三四五六日天]|星期[一二三四五六日天])(?:前)?|今天|明天|后天|今晚|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]?)"
)

TASK_KEYWORDS = (
    "负责",
    "完成",
    "准备",
    "处理",
    "提交",
    "确认",
    "同步",
    "推进",
    "修复",
    "整理",
    "开发",
    "联调",
    "后端",
    "前端",
    "海报",
    "材料",
    "demo",
    "路演",
    "文档",
    "表格",
    "搞",
    "做",
)

GENERIC_TITLES = {"搞定", "完成", "处理", "安排", "推进", "准备", "开发"}


def extract_tasks(raw_text: str) -> list[TaskItem]:
    clauses = _extract_candidate_clauses(raw_text)
    tasks: list[TaskItem] = []
    for clause in clauses:
        task = _parse_clause(clause)
        if task:
            tasks.append(task)
    return _dedupe_tasks(tasks)


def normalize_tasks(tasks: list[TaskItem]) -> list[TaskItem]:
    normalized: list[TaskItem] = []
    for task in tasks:
        normalized.append(
            TaskItem(
                title=_normalize_title(task.title) or "待确认任务",
                owner=_clean_owner(task.owner) or "TBD",
                priority=task.priority if task.priority in {"high", "medium", "low"} else "medium",
                due_date=(task.due_date or "TBD").strip() or "TBD",
                status=(task.status or "draft").strip() or "draft",
                notes=" ".join((task.notes or "").split()),
            )
        )
    return normalized


def apply_discussion_updates(tasks: list[TaskItem], raw_text: str) -> list[TaskItem]:
    if not tasks:
        return tasks

    updated_tasks = [task.model_copy(deep=True) for task in tasks]
    for signal in _extract_update_signals(raw_text):
        target_index = _find_update_target(updated_tasks, signal)
        if target_index is None:
            continue

        target = updated_tasks[target_index]
        notes = target.notes or ""
        extra_note = signal["note"]
        if extra_note and extra_note not in notes:
            notes = f"{notes}；更新：{extra_note}".strip("；")

        title_hint = str(signal.get("title_hint") or "").strip()
        new_title = (
            title_hint
            if title_hint and title_hint not in {"待确认任务", "__UPDATE__"}
            else target.title
        )

        updated_tasks[target_index] = target.model_copy(
            update={
                "title": new_title,
                "priority": _pick_higher_priority(target.priority, signal["priority"]),
                "due_date": signal["due_date"] or target.due_date,
                "notes": notes,
            }
        )

    return updated_tasks


def infer_risks(tasks: list[TaskItem]) -> list[str]:
    risks: list[str] = []
    for task in tasks:
        if task.owner == "TBD":
            risks.append(f"任务《{task.title}》尚未明确负责人。")
        if task.due_date == "TBD":
            risks.append(f"任务《{task.title}》尚未明确截止时间。")
        if "已过期" in task.due_date:
            risks.append(f"任务《{task.title}》的截止时间已过期，建议立即确认新的排期。")
        if len(task.title.strip()) <= 2 or task.title.strip() in GENERIC_TITLES:
            risks.append(f"任务《{task.title}》描述偏简略，建议再补充细节。")
    return _unique(risks)


def build_summary(raw_text: str, tasks: list[TaskItem]) -> str:
    clauses = _extract_candidate_clauses(raw_text)
    if not clauses and not tasks:
        return "最近的群聊里还没有形成明确的协作任务。"

    owner_count = len({task.owner for task in tasks if task.owner != "TBD"})
    if tasks:
        return f"已从最近一轮讨论中整理出 {len(tasks)} 项协作任务，涉及 {owner_count} 位已明确负责人。"
    return f"最近讨论主要是状态同步，共有 {len(clauses)} 条有效讨论片段，暂未抽取出明确任务。"


def build_next_actions(tasks: list[TaskItem], risks: Iterable[str]) -> list[str]:
    actions: list[str] = []
    for task in tasks:
        if task.owner == "TBD":
            actions.append(f"尽快确认《{task.title}》的负责人。")
        elif task.due_date == "TBD":
            actions.append(f"请 {task.owner} 补充《{task.title}》的明确截止时间。")
        elif "已过期" in task.due_date:
            actions.append(f"请尽快重排《{task.title}》的时间，并同步给 {task.owner}。")
        else:
            actions.append(f"请 {task.owner} 按计划推进《{task.title}》，目标日期 {task.due_date}。")

    if not actions:
        actions.append("在群里继续补充任务分工、截止时间和阻塞项。")

    if list(risks) and len(actions) < 3:
        actions.append("针对当前风险项再确认一次优先级和资源安排。")

    return _unique(actions)[:4]


def _extract_candidate_clauses(raw_text: str) -> list[str]:
    clauses: list[str] = []
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line = SENDER_PREFIX_RE.sub("", line)
        line = line.lstrip("-* ").strip()
        line = LEADING_FILLER_RE.sub("", line).strip()
        if not line:
            continue

        if "近期群聊讨论" in line and ":" in line:
            line = line.split(":", 1)[1].strip()

        clauses.extend(_split_multi_assignment(line))

    return [clause for clause in clauses if _looks_like_action_clause(clause)]


def _split_multi_assignment(text: str) -> list[str]:
    segments = [segment.strip() for segment in re.split(r"[，；]", text) if segment.strip()]
    if len(segments) <= 1:
        return [text.strip()]

    clauses: list[str] = []
    current = segments[0]
    for segment in segments[1:]:
        if _looks_like_new_owner_segment(segment):
            clauses.append(current.strip())
            current = segment
        else:
            current = f"{current}，{segment}"
    clauses.append(current.strip())
    return clauses


def _looks_like_new_owner_segment(text: str) -> bool:
    compact = LEADING_FILLER_RE.sub("", text.strip())
    if not compact:
        return False
    if any(token in compact for token in ("今天", "明天", "后天", "本周", "这周", "下周", "周", "星期", "需要", "大概", "预计")):
        return False
    return bool(
        re.match(
            r"^[\u4e00-\u9fa5A-Za-z0-9]{1,12}(?:你|同学|老师)?(?:负责|完成|准备|处理|跟进|推进|搞|做)",
            compact,
        )
    )


def _looks_like_action_clause(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in TASK_KEYWORDS)


def _parse_clause(clause: str) -> TaskItem | None:
    text = LEADING_FILLER_RE.sub("", clause.strip()).strip("，。；; ")
    if not text:
        return None

    owner = _extract_owner(text)
    due_date = _extract_due_hint(text)
    priority = "high" if any(word in text for word in ("紧急", "很紧急", "尽快", "马上", "立即")) else "medium"
    title = _extract_title(text, owner, due_date)
    if not title:
        return None

    return TaskItem(
        title=title,
        owner=owner or "TBD",
        priority=priority,
        due_date=due_date or "TBD",
        status="draft",
        notes=text,
    )


def _extract_update_signals(raw_text: str) -> list[dict[str, str]]:
    signals: list[dict[str, str]] = []
    for raw_line in (raw_text or "").splitlines():
        line = SENDER_PREFIX_RE.sub("", raw_line.strip())
        line = line.lstrip("-* ").strip()
        line = LEADING_FILLER_RE.sub("", line).strip()
        if not line:
            continue

        owner = _extract_owner(line)
        due_date = _extract_due_hint(line)
        is_update = any(token in line for token in ("不对", "改成", "调整", "提前", "推迟", "延期", "紧急", "很紧急"))
        if owner == "TBD" or (due_date == "TBD" and not is_update):
            continue

        title_hint = _extract_title(line, owner, due_date)
        if not title_hint and not is_update:
            continue

        signals.append(
            {
                "owner": owner,
                "due_date": due_date,
                "priority": "high" if any(word in line for word in ("紧急", "很紧急", "尽快", "马上", "立即")) else "medium",
                "title_hint": title_hint or "__UPDATE__",
                "note": line,
            }
        )
    return signals


def _find_update_target(tasks: list[TaskItem], signal: dict[str, str]) -> int | None:
    owner = signal["owner"]
    title_hint = str(signal.get("title_hint") or "")
    same_owner_indices = [idx for idx, task in enumerate(tasks) if task.owner == owner]
    if not same_owner_indices:
        return None

    if title_hint and title_hint not in {"待确认任务", "__UPDATE__"}:
        for idx in reversed(same_owner_indices):
            task = tasks[idx]
            if task.title == title_hint or title_hint in task.title or task.title in title_hint:
                return idx

    return same_owner_indices[-1]


def _pick_higher_priority(current: str, incoming: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    current_value = order.get((current or "medium").lower(), 1)
    incoming_value = order.get((incoming or "medium").lower(), 1)
    return incoming if incoming_value > current_value else current


def _extract_owner(text: str) -> str:
    match = DIALOG_OWNER_RE.match(text) or ACTION_OWNER_RE.match(text)
    if not match:
        return "TBD"

    owner = match.group("owner").strip()
    invalid_tokens = {"今天", "明天", "后天", "本周", "这周", "下周", "需要", "大概", "预计"}
    if owner in invalid_tokens:
        return "TBD"
    if any(token in owner for token in ("周", "今天", "明天", "后天", "需要", "大概", "预计")):
        return "TBD"
    return owner


def _extract_due_hint(text: str) -> str:
    match = DUE_HINT_RE.search(text)
    return match.group(0) if match else "TBD"


def _extract_title(text: str, owner: str, due_hint: str) -> str:
    working = text
    if owner and owner != "TBD":
        working = re.sub(rf"^{re.escape(owner)}(?:你|同学|老师)?", "", working).strip()
    if due_hint and due_hint != "TBD":
        working = working.replace(due_hint, " ")

    working = re.sub(r"(大概|预计|需要你|需要|尽快|马上|立即|搞定|完成|负责|本周|这周|下周)", " ", working)
    working = re.sub(r"[，。；;：:]", " ", working)
    working = " ".join(working.split())
    raw_working = working

    keyword_map = {
        "后端": "后端开发",
        "前端": "前端开发",
        "联调": "飞书联调",
        "海报": "海报确认",
        "报名材料": "报名材料提交",
        "路演": "路演 Demo 准备",
        "demo": "路演 Demo 准备",
        "汇报": "汇报材料整理",
        "文档": "文档整理",
        "表格": "多维表格同步",
    }
    lowered = working.lower()
    for keyword, title in keyword_map.items():
        if keyword.lower() in lowered:
            return title

    if not any(keyword in text.lower() or keyword in text for keyword in TASK_KEYWORDS):
        return ""

    working = re.sub(r"^(开发|准备|提交|确认|处理|推进|整理|同步|修复)\s*", "", working)
    working = working.strip()
    if not working:
        return raw_working.strip()

    if len(working) <= 8:
        for suffix in ("开发", "联调", "准备", "提交", "确认", "整理"):
            if suffix in text and not working.endswith(suffix):
                working = f"{working}{suffix}"
                break

    return _normalize_title(working)


def _normalize_title(title: str) -> str:
    value = " ".join((title or "").split()).strip("，。；; ")
    if value in GENERIC_TITLES:
        return "待确认任务"
    return value


def _clean_owner(owner: str) -> str:
    value = (owner or "").strip()
    if not value or value in {"大概下", "需要你", "这周", "下周"}:
        return "TBD"
    return value


def _dedupe_tasks(tasks: list[TaskItem]) -> list[TaskItem]:
    seen: set[tuple[str, str, str]] = set()
    result: list[TaskItem] = []
    for task in tasks:
        key = (task.title, task.owner, task.notes)
        if key in seen:
            continue
        seen.add(key)
        result.append(task)
    return result


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
