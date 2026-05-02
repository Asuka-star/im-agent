import unittest

from app.services.workflow.persistence import WorkflowResultPersistence


class _TaskRunService:
    def __init__(self) -> None:
        self.artifacts: list[dict] = []
        self.steps: list[dict] = []

    def create_artifact(self, task_run_id: str, **kwargs) -> None:
        self.artifacts.append({"task_run_id": task_run_id, **kwargs})

    def upsert_step(self, task_run_id: str, **kwargs) -> None:
        self.steps.append({"task_run_id": task_run_id, **kwargs})


class _Workflow:
    def __init__(self) -> None:
        self.task_run_service = _TaskRunService()


class WorkflowResultPersistenceTests(unittest.TestCase):
    def test_persist_slides_records_rehearsal_step(self) -> None:
        workflow = _Workflow()
        service = WorkflowResultPersistence(workflow)

        service.persist_artifacts(
            "run_rehearsal",
            [
                {
                    "artifact_type": "slides_package",
                    "title": "汇报 PPT",
                    "preview": {
                        "slides": [
                            {
                                "title": "开场",
                                "bullets": ["问题", "目标"],
                                "speaker_notes": "30 秒说明背景。",
                                "duration_sec": 30,
                            },
                            {
                                "title": "方案",
                                "bullets": ["1", "2", "3", "4", "5", "6"],
                            },
                        ]
                    },
                }
            ],
        )

        self.assertEqual(len(workflow.task_run_service.artifacts), 1)
        self.assertEqual(len(workflow.task_run_service.steps), 1)
        step = workflow.task_run_service.steps[0]
        self.assertEqual(step["step_key"], "slides_rehearsal_prepared")
        self.assertEqual(step["title"], "生成排练建议")
        self.assertEqual(step["status"], "done")
        self.assertEqual(step["output_payload"]["slide_count"], 2)
        self.assertEqual(step["output_payload"]["speaker_notes_count"], 1)
        self.assertEqual(step["output_payload"]["duration_sec"], 30)
        self.assertEqual(step["output_payload"]["missing_notes_pages"], [2])
        self.assertEqual(step["output_payload"]["dense_slide_pages"], [2])

    def test_non_slides_artifact_does_not_record_rehearsal_step(self) -> None:
        workflow = _Workflow()
        service = WorkflowResultPersistence(workflow)

        service.persist_artifacts(
            "run_canvas",
            [{"artifact_type": "canvas", "title": "流程图", "preview": {"shapes": []}}],
        )

        self.assertEqual(len(workflow.task_run_service.artifacts), 1)
        self.assertEqual(workflow.task_run_service.steps, [])


if __name__ == "__main__":
    unittest.main()
