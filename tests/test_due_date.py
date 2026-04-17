import unittest
from datetime import date

from app.schemas.task import TaskItem
from app.services.due_date import normalize_due_date_text, normalize_task_dates


class DueDateTests(unittest.TestCase):
    def test_normalize_relative_weekday(self) -> None:
        result = normalize_due_date_text("下周四搞定", today=date(2026, 4, 17))
        self.assertEqual(result, "2026-04-23")

    def test_past_weekday_is_annotated(self) -> None:
        result = normalize_due_date_text("这周二搞定", today=date(2026, 4, 17))
        self.assertIn("2026-04-14", result)
        self.assertIn("已过期", result)

    def test_notes_can_override_stale_due_date(self) -> None:
        task = TaskItem(
            title="后端开发",
            owner="张三",
            priority="medium",
            due_date="2024-06-11",
            status="draft",
            notes="张三你去搞后端，大概下周四搞定",
        )
        normalized = normalize_task_dates([task], today=date(2026, 4, 17))
        self.assertEqual(normalized[0].due_date, "2026-04-23")


if __name__ == "__main__":
    unittest.main()
