import re
from collections import Counter

from app.schemas.task import TaskItem

OWNER_FRAGMENT = r"[A-Za-z][A-Za-z0-9_-]{0,20}|[\u4e00-\u9fff]{1,8}"
ACTION_VERBS = (
    "完成",
    "准备",
    "处理",
    "提交",
    "联调",
    "跟进",
    "整理",
    "输出",
    "验证",
    "推进",
    "修复",
    "review",
    "prepare",
    "finish",
    "verify",
    "handle",
    "sync",
    "write",
    "build",
)
HIGH_PRIORITY_MARKERS = ("今天", "今晚", "尽快", "立即", "马上", "asap", "today", "tonight", "urgent")
MEDIUM_PRIORITY_MARKERS = ("本周", "这周", "周", "this week", "before friday", "friday")
INVALID_OWNER_TOKENS = {
    "请",
    "需要",
    "今天",
    "明天",
    "后天",
    "今晚",
    "本周",
    "这周",
    "下周",
    "周一",
    "周二",
    "周三",
    "周四",
    "周五",
    "周六",
    "周日",
    "today",
    "tomorrow",
    "tonight",
    "this week",
}


def extract_tasks(raw_text: str) -> list[TaskItem]:
    clauses = split_clauses(raw_text)
    tasks: list[TaskItem] = []

    for clause in clauses:
        task = _extract_task_from_clause(clause)
        if task is not None:
            tasks.append(task)

    if tasks:
        return tasks

    fallback_notes = raw_text.strip()[:200]
    if not fallback_notes:
        fallback_notes = "No discussion text provided."

    return [
        TaskItem(
            title="Clarify the latest discussion and identify concrete follow-up",
            owner="TBD",
            priority="low",
            due_date="TBD",
            status="draft",
            notes=fallback_notes,
        )
    ]


def build_summary(raw_text: str, tasks: list[TaskItem]) -> str:
    if not tasks:
        return "No concrete task was identified from the latest discussion."

    owner_count = len({task.owner for task in tasks if task.owner != "TBD"})
    preview = raw_text.strip().replace("\n", " ")
    preview = re.sub(r"\s+", " ", preview)
    preview = preview[:80]
    return (
        f"Identified {len(tasks)} task(s) from the latest discussion, involving "
        f"{owner_count} named owner(s). Source preview: {preview}"
    )


def build_next_actions(tasks: list[TaskItem], risks: list[str]) -> list[str]:
    actions: list[str] = []

    if any(task.owner == "TBD" for task in tasks):
        actions.append("Confirm the owner for tasks that still have no assigned person.")

    if any(task.due_date == "TBD" for task in tasks):
        actions.append("Confirm concrete due dates for tasks that still have no deadline.")

    if risks:
        actions.append("Review the highlighted risks in the Feishu group and resolve the ambiguous items.")

    if not actions:
        actions.append("Confirm the generated task list in the Feishu group.")

    actions.append("Send a follow-up message after updates so the assistant can refresh the task view.")
    return actions[:4]


def infer_risks(tasks: list[TaskItem]) -> list[str]:
    risks: list[str] = []

    for task in tasks:
        if task.owner == "TBD":
            risks.append(f"Task '{task.title}' still has no confirmed owner.")
        if task.due_date == "TBD":
            risks.append(f"Task '{task.title}' still has no confirmed due date.")
        if len(task.title) < 8:
            risks.append(f"Task '{task.title}' may be too vague and should be clarified.")

    owner_counter = Counter(task.owner for task in tasks if task.owner != "TBD")
    overloaded = [owner for owner, count in owner_counter.items() if count >= 3]
    for owner in overloaded:
        risks.append(f"Owner '{owner}' currently has many tasks and may need rebalancing.")

    if not risks:
        risks.append("The current task plan looks coherent, but owners and due dates should still be confirmed in chat.")

    return risks


def normalize_tasks(tasks: list[TaskItem]) -> list[TaskItem]:
    normalized: list[TaskItem] = []
    for task in tasks:
        notes = re.sub(r"\s+", " ", task.notes).strip()
        normalized.append(
            task.model_copy(
                update={
                    "title": task.title.strip().rstrip("。.;,"),
                    "owner": task.owner.strip() or "TBD",
                    "priority": task.priority.strip() or "medium",
                    "due_date": task.due_date.strip() or "TBD",
                    "notes": notes,
                }
            )
        )
    return normalized


def split_clauses(raw_text: str) -> list[str]:
    normalized = raw_text.replace("\r", "\n")
    normalized = re.sub(r"\n+", "\n", normalized)
    chunks = re.split(r"[。\n；;]+", normalized)

    clauses: list[str] = []
    for chunk in chunks:
        for clause in re.split(r"[，,]+", chunk):
            cleaned = re.sub(r"\s+", " ", clause).strip()
            if cleaned:
                clauses.append(cleaned)
    return clauses


def _extract_task_from_clause(clause: str) -> TaskItem | None:
    owner_due_patterns = [
        re.compile(
            rf"^(?P<owner>{OWNER_FRAGMENT})在(?P<due>[^。；;，,]{{1,20}})前(?P<action>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^(?P<owner>{OWNER_FRAGMENT})(?P<due>今天|明天|今晚|本周|这周|下周|周一|周二|周三|周四|周五|周六|周日|Friday|Monday|Tuesday|Wednesday|Thursday|Saturday|Sunday)前?(?P<action>.+)$",
            re.IGNORECASE,
        ),
    ]
    owner_action_patterns = [
        re.compile(rf"^(?P<owner>{OWNER_FRAGMENT})负责(?P<action>.+)$", re.IGNORECASE),
        re.compile(rf"^(?P<owner>{OWNER_FRAGMENT})(?: will | needs to | should | to )(?P<action>.+)$", re.IGNORECASE),
        re.compile(
            rf"^(?P<owner>{OWNER_FRAGMENT})(?P<action>(?:{'|'.join(ACTION_VERBS)}).+)$",
            re.IGNORECASE,
        ),
    ]

    for pattern in owner_due_patterns:
        match = pattern.match(clause)
        if match:
            owner = _normalize_owner(match.group("owner"))
            due = match.group("due")
            action = _clean_action(match.group("action"))
            return TaskItem(
                title=action,
                owner=owner,
                priority=infer_priority(f"{due} {action}"),
                due_date=_clean_due_date(due),
                status="draft",
                notes=clause,
            )

    for pattern in owner_action_patterns:
        match = pattern.match(clause)
        if match:
            owner = _normalize_owner(match.group("owner"))
            action = _clean_action(match.group("action"))
            due_date = extract_due_date(clause)
            return TaskItem(
                title=action,
                owner=owner,
                priority=infer_priority(clause),
                due_date=due_date,
                status="draft",
                notes=clause,
            )

    if _looks_like_action_clause(clause):
        return TaskItem(
            title=_clean_action(clause),
            owner="TBD",
            priority=infer_priority(clause),
            due_date=extract_due_date(clause),
            status="draft",
            notes=clause,
        )

    return None


def extract_due_date(text: str) -> str:
    patterns = [
        r"(\d{4}-\d{1,2}-\d{1,2}(?:前)?)",
        r"(\d{1,2}月\d{1,2}日(?:前)?)",
        r"(今天|明天|后天|今晚|本周|这周|下周|月底|月末)",
        r"(周一|周二|周三|周四|周五|周六|周日)前?",
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:\s+EOD|\s+morning|\s+afternoon)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _clean_due_date(match.group(1))
    return "TBD"


def infer_priority(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in HIGH_PRIORITY_MARKERS):
        return "high"
    if any(marker in lowered for marker in MEDIUM_PRIORITY_MARKERS):
        return "medium"
    return "low"


def _looks_like_action_clause(clause: str) -> bool:
    keywords = (
        "需要",
        "请",
        "确认",
        "跟进",
        "提交",
        "准备",
        "完成",
        "联调",
        "验证",
        "处理",
        "修复",
        "review",
        "prepare",
        "finish",
        "verify",
        "handle",
    )
    lowered = clause.lower()
    return any(keyword in clause or keyword in lowered for keyword in keywords)


def _clean_action(action: str) -> str:
    cleaned = re.sub(r"\s+", " ", action).strip()
    cleaned = cleaned.lstrip(":：- ")
    return cleaned[:120] if cleaned else "Clarify follow-up"


def _clean_due_date(due_date: str) -> str:
    return re.sub(r"\s+", " ", due_date).strip().rstrip("。.;,")


def _normalize_owner(owner: str) -> str:
    cleaned = re.sub(r"\s+", " ", owner).strip()
    if cleaned.lower() in INVALID_OWNER_TOKENS or cleaned in INVALID_OWNER_TOKENS:
        return "TBD"
    return cleaned or "TBD"
