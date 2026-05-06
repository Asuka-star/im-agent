import unittest

from app.schemas.task import TaskItem
from app.services.text_analysis import (
    apply_discussion_updates,
    build_summary,
    extract_tasks,
    infer_risks,
    normalize_tasks,
)


class TextAnalysisTests(unittest.TestCase):
    def test_extract_tasks_from_assignment(self) -> None:
        tasks = normalize_tasks(extract_tasks("张三你去搞后端，大概下周四搞定"))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "张三")
        self.assertEqual(tasks[0].title, "后端开发")

    def test_apply_discussion_updates_adjusts_existing_task(self) -> None:
        tasks = [
            TaskItem(
                title="后端开发",
                owner="张三",
                priority="medium",
                due_date="下周四",
                status="draft",
                notes="张三你去搞后端，大概下周四搞定",
            )
        ]
        updated = apply_discussion_updates(tasks, "不对，张三这个很紧急，需要你这周五之前搞定")
        self.assertEqual(updated[0].priority, "high")
        self.assertEqual(updated[0].owner, "张三")
        self.assertIn("周五", updated[0].due_date)

    def test_infer_risks_for_tbd_owner_and_generic_title(self) -> None:
        risks = infer_risks(
            [
                TaskItem(
                    title="搞定",
                    owner="TBD",
                    priority="medium",
                    due_date="TBD",
                    status="draft",
                    notes="尽快搞定",
                )
            ]
        )
        self.assertTrue(any("负责人" in risk for risk in risks))
        self.assertTrue(any("截止时间" in risk for risk in risks))
        self.assertTrue(any("描述偏简略" in risk for risk in risks))

    def test_build_summary_uses_clean_chinese(self) -> None:
        tasks = normalize_tasks(extract_tasks("李四你去搞前端，需要你下周三前搞定"))
        summary = build_summary("李四你去搞前端，需要你下周三前搞定", tasks)
        self.assertIn("最近一轮讨论", summary)
        self.assertIn("1 项协作任务", summary)

    def test_extract_tasks_ignores_sink_request_to_table(self) -> None:
        tasks = normalize_tasks(extract_tasks("帮我把刚才讨论同步到表格"))
        self.assertEqual(tasks, [])

    def test_extract_tasks_from_come_do_assignment(self) -> None:
        tasks = normalize_tasks(extract_tasks("王五来做产品经理，来协调前端和后端的开发"))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "王五")
        self.assertEqual(tasks[0].title, "产品经理协调前后端开发")

    def test_extract_tasks_from_dialog_assignment_with_pause(self) -> None:
        tasks = normalize_tasks(extract_tasks("张三，你同时去搞一下录屏"))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "张三")
        self.assertEqual(tasks[0].title, "录屏")

    def test_extract_tasks_from_additional_assignee_assignment(self) -> None:
        tasks = normalize_tasks(extract_tasks("王五你也去搞一下录屏"))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "王五")
        self.assertEqual(tasks[0].title, "录屏")

    def test_extract_tasks_from_rewritten_mention_assignment(self) -> None:
        tasks = normalize_tasks(extract_tasks("zero你来帮我搞一下前端开发"))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].owner, "zero")
        self.assertEqual(tasks[0].title, "前端开发")


if __name__ == "__main__":
    unittest.main()
