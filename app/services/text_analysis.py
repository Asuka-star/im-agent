import re
from typing import Iterable

from app.schemas.task import TaskItem


SENDER_PREFIX_RE = re.compile(r"^\s*[-*]?\s*[A-Za-z0-9_]{4,}:\s*")
LEADING_FILLER_RE = re.compile(r"^(不对|另外|然后|还有|补充一下|补充|顺便|以及|再|那|这个|目前|近期群聊讨论)\s*[，,：:]?")
DIALOG_OWNER_RE = re.compile(r"^(?P<owner>[\u4e00-\u9fa5A-Za-z0-9]{1,4})(?=你|同学|老师|这边)")
ACTION_OWNER_RE = re.compile(
    r"^(?P<owner>[\u4e00-\u9fa5A-Za-z0-9]{1,4})(?=负责|去|完成|准备|处理|搞|做|跟进|推进|这个)"
)
DUE_HINT_RE = re.compile(
    r"(今天|明天|后天|今晚|本周|这周|下周(?:[一二三四五六日天])(?:前)?|"
    r"(?:本周|这周)(?:[一二三四五六日天])(?:前)?|(?:周|星期)[一二三四五六日天](?:前)?|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日)"
)


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
        title = _normalize_title(task.title)
        owner = _clean_owner(task.owner)
        notes = " ".join((task.notes or "").split())
        priority = task.priority if task.priority in {"high", "medium", "low"} else "medium"
        due_date = (task.due_date or "TBD").strip() or "TBD"
        status = (task.status or "draft").strip() or "draft"

        normalized.append(
            TaskItem(
                title=title or "待确认任务",
                owner=owner or "TBD",
                priority=priority,
                due_date=due_date,
                status=status,
                notes=notes,
            )
        )
    return normalized


def infer_risks(tasks: list[TaskItem]) -> list[str]:
    risks: list[str] = []
    for task in tasks:
        if task.owner == "TBD":
            risks.append(f"任务《{task.title}》尚未明确负责人。")
        if task.due_date == "TBD":
            risks.append(f"任务《{task.title}》尚未明确截止时间。")
        if "已过期" in task.due_date:
            risks.append(f"任务《{task.title}》的截止时间已过期，建议立即确认新的排期。")
        if len(task.title.strip()) <= 2 or task.title.strip() in {"搞定", "处理", "完成", "安排"}:
            risks.append(f"任务《{task.title}》描述过于笼统，建议补充更具体的交付内容。")

    return _unique(risks)


def build_summary(raw_text: str, tasks: list[TaskItem]) -> str:
    clauses = _extract_candidate_clauses(raw_text)
    if not clauses and not tasks:
        return "最近的群聊里还没有形成明确的协作任务。"

    owner_count = len({task.owner for task in tasks if task.owner != "TBD"})
    if tasks:
        return (
            f"已从最近一轮讨论中整理出 {len(tasks)} 项协作任务，"
            f"涉及 {owner_count} 位已明确负责人。"
        )
    return f"最近讨论主要是状态沟通，共有 {len(clauses)} 条有效讨论片段，暂未抽取出明确任务。"


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

        parts = [part.strip() for part in re.split(r"[；;]", line) if part.strip()]
        for part in parts:
            clauses.extend(_split_multi_assignment(part))

    return [clause for clause in clauses if _looks_like_action_clause(clause)]


def _split_multi_assignment(text: str) -> list[str]:
    segments = [segment.strip() for segment in re.split(r"[，,]", text) if segment.strip()]
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
            r"^[\u4e00-\u9fa5A-Za-z0-9]{1,12}(?:你|同学|老师)?(?:去|负责|完成|准备|处理|搞|做|跟进|推进)",
            compact,
        )
    )


def _looks_like_action_clause(text: str) -> bool:
    return bool(
        re.search(r"(负责|完成|准备|处理|提交|确认|同步|推进|修复|整理|开发|联调|后端|前端|海报|材料|demo|路演)", text, re.IGNORECASE)
        or re.search(r"(去搞|去做)", text)
    )


def _parse_clause(clause: str) -> TaskItem | None:
    text = LEADING_FILLER_RE.sub("", clause.strip()).strip("，,。；; ")
    if not text:
        return None

    owner = _extract_owner(text)
    due_date = _extract_due_hint(text)
    priority = "high" if any(word in text for word in ("紧急", "尽快", "马上", "立即")) else "medium"
    title = _extract_title(text, owner, due_date)
    notes = text

    if not title:
        return None

    return TaskItem(
        title=title,
        owner=owner or "TBD",
        priority=priority,
        due_date=due_date or "TBD",
        status="draft",
        notes=notes,
    )


def _extract_owner(text: str) -> str:
    match = DIALOG_OWNER_RE.match(text) or ACTION_OWNER_RE.match(text)
    if not match:
        return "TBD"

    owner = match.group("owner").strip()

    if owner in {"今天", "明天", "后天", "本周", "这周", "下周", "需要", "大概", "预计"}:
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

    working = re.sub(r"(大概|预计|需要你|需要|尽快|马上|立即|搞定|完成|负责|去|做|搞|一下|这周|本周|下周)", " ", working)
    working = re.sub(r"[，,。；;：:]", " ", working)
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

    if not re.search(r"(开发|联调|准备|提交|确认|整理|修复|同步|处理|后端|前端|海报|材料|demo|路演)", text, re.IGNORECASE):
        return ""

    working = re.sub(r"^(开发|准备|提交|确认|处理|推进|整理|同步|修复)\s*", "", working)
    working = working.strip()
    if not working:
        return raw_working.strip()

    if len(working) <= 8 and any(word in text for word in ("开发", "联调", "准备", "提交", "确认", "整理")):
        suffix = next(
            (s for s in ("开发", "联调", "准备", "提交", "确认", "整理") if s in text),
            "",
        )
        if suffix and not working.endswith(suffix):
            working = f"{working}{suffix}"

    return _normalize_title(working)


def _normalize_title(title: str) -> str:
    value = " ".join((title or "").split()).strip("，,。；; ")
    bad_values = {"搞定", "完成", "处理", "安排", "推进", "准备", "开发"}
    if value in bad_values:
        return "待确认任务"
    return value


def _clean_owner(owner: str) -> str:
    value = (owner or "").strip()
    if not value or value in {"大概下", "需要你下", "需要你在", "需要你", "这周", "下周"}:
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
