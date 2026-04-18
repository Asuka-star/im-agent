import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas.task import TaskItem


WEEKDAY_MAP = {
    "周一": 0,
    "星期一": 0,
    "周二": 1,
    "星期二": 1,
    "周三": 2,
    "星期三": 2,
    "周四": 3,
    "星期四": 3,
    "周五": 4,
    "星期五": 4,
    "周六": 5,
    "星期六": 5,
    "周日": 6,
    "星期日": 6,
    "周天": 6,
    "星期天": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

RELATIVE_TIME_MARKERS = (
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
    "周天",
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
    "星期天",
    "today",
    "tomorrow",
    "tonight",
    "this week",
    "next week",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

MONTH_DAY_RE = re.compile(r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?")


def current_local_date() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def normalize_due_date_text(text: str, *, today: date | None = None) -> str:
    candidate = (text or "").strip()
    if not candidate or candidate.upper() == "TBD":
        return "TBD"

    today = today or current_local_date()
    parsed = (
        _parse_iso_date(candidate)
        or _parse_month_day(candidate, today)
        or _parse_relative_day(candidate, today)
        or _parse_weekday(candidate, today)
    )
    if parsed is None:
        return candidate
    if parsed < today:
        return _format_past_due_candidate(candidate, parsed)
    return parsed.isoformat()


def normalize_task_dates(tasks: list[TaskItem], *, today: date | None = None) -> list[TaskItem]:
    today = today or current_local_date()
    normalized: list[TaskItem] = []

    for task in tasks:
        due_from_field = normalize_due_date_text(task.due_date, today=today)
        due_from_notes = normalize_due_date_text(task.notes, today=today)

        chosen_due_date = due_from_field
        if _should_prefer_note_date(
            task=task,
            due_from_field=due_from_field,
            due_from_notes=due_from_notes,
            today=today,
        ):
            chosen_due_date = due_from_notes

        normalized.append(task.model_copy(update={"due_date": chosen_due_date}))

    return normalized


def contains_relative_time_reference(text: str) -> bool:
    candidate = (text or "").strip()
    lowered = candidate.lower()
    return any(marker in candidate or marker in lowered for marker in RELATIVE_TIME_MARKERS) or bool(MONTH_DAY_RE.search(candidate))


def _should_prefer_note_date(
    *,
    task: TaskItem,
    due_from_field: str,
    due_from_notes: str,
    today: date,
) -> bool:
    if due_from_notes == "TBD":
        return False
    if not contains_relative_time_reference(task.notes):
        return False
    if due_from_field == "TBD":
        return True
    return _looks_like_unreasonable_absolute_date(due_from_field, today=today)


def _looks_like_unreasonable_absolute_date(value: str, *, today: date) -> bool:
    parsed = _extract_iso_date(value)
    if parsed is None:
        return False
    return parsed.year < today.year or parsed.year > today.year + 1


def _extract_iso_date(text: str) -> date | None:
    match = re.search(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})", text)
    if not match:
        return None
    return _safe_date(int(match.group("year")), int(match.group("month")), int(match.group("day")))


def _parse_iso_date(text: str) -> date | None:
    match = re.search(r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})", text)
    if not match:
        return None
    return _safe_date(int(match.group("year")), int(match.group("month")), int(match.group("day")))


def _parse_month_day(text: str, today: date) -> date | None:
    match = MONTH_DAY_RE.search(text)
    if not match:
        return None

    year = int(match.group("year")) if match.group("year") else today.year
    parsed = _safe_date(year, int(match.group("month")), int(match.group("day")))
    if parsed is None:
        return None

    if not match.group("year") and parsed < today - timedelta(days=30):
        return _safe_date(year + 1, int(match.group("month")), int(match.group("day")))
    return parsed


def _parse_relative_day(text: str, today: date) -> date | None:
    lowered = text.lower()
    if "今天" in text or "today" in lowered:
        return today
    if "明天" in text or "tomorrow" in lowered:
        return today + timedelta(days=1)
    if "后天" in text:
        return today + timedelta(days=2)
    if "今晚" in text or "tonight" in lowered:
        return today
    return None


def _parse_weekday(text: str, today: date) -> date | None:
    lowered = text.lower()
    for token, weekday in WEEKDAY_MAP.items():
        if token not in text and token not in lowered:
            continue

        current_week_start = today - timedelta(days=today.weekday())
        if "下周" in text or "next week" in lowered:
            week_start = current_week_start + timedelta(days=7)
        elif "本周" in text or "这周" in text or "this week" in lowered:
            week_start = current_week_start
        elif token.startswith("周") or token.startswith("星期"):
            week_start = current_week_start
            if weekday < today.weekday():
                week_start = current_week_start + timedelta(days=7)
        else:
            week_start = current_week_start

        return week_start + timedelta(days=weekday)
    return None


def _format_past_due_candidate(original_text: str, parsed: date) -> str:
    suggestion = ""
    if _contains_weekday_reference(original_text):
        suggestion = f"，请确认是否应为 {(parsed + timedelta(days=7)).isoformat()}"
    return f"{parsed.isoformat()}（已过期{suggestion}）"


def _contains_weekday_reference(text: str) -> bool:
    lowered = text.lower()
    return any(token in text or token in lowered for token in WEEKDAY_MAP)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
