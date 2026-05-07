import unittest
from types import SimpleNamespace

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


class _MemoryService:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save_assistant_message(self, **kwargs) -> None:
        self.saved.append(kwargs)


class _RequirementService:
    def __init__(self) -> None:
        self.updated: list[str] = []

    def update_current_artifacts_from_task_run(self, task_run_id: str) -> None:
        self.updated.append(task_run_id)


class _FeishuArtifactIntegrator:
    def __init__(self) -> None:
        self.synced: list[object] = []

    def sync_current_task_run_artifacts(self, detail) -> None:
        self.synced.append(detail)


class _TaskRunServiceForPersist(_TaskRunService):
    def __init__(self) -> None:
        super().__init__()
        self.updated_runs: list[dict] = []

    def update_task_run(self, task_run_id: str, **kwargs) -> None:
        self.updated_runs.append({"task_run_id": task_run_id, **kwargs})

    def get_task_run(self, task_run_id: str):
        return SimpleNamespace(task_run_id=task_run_id, session_id="session_1")


class _WorkflowForPersist:
    def __init__(self) -> None:
        self.task_run_service = _TaskRunServiceForPersist()
        self.memory_service = _MemoryService()
        self.requirement_service = _RequirementService()
        self.feishu_artifact_integrator = _FeishuArtifactIntegrator()

    def _condense_text(self, value: object) -> str:
        return str(value or "")

    def _task_run_title(self, message_text: str, mode: str) -> str:
        return f"{mode}:{message_text}"


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

    def test_persist_task_run_result_syncs_current_slides_or_canvas_to_feishu(self) -> None:
        workflow = _WorkflowForPersist()
        service = WorkflowResultPersistence(workflow)

        service.persist_task_run_result(
            "run_sync",
            message_text="更新演示稿",
            session_id="session_1",
            result={
                "mode": "slides",
                "reply_preview": "已更新演示稿",
                "analysis": None,
                "artifacts": [
                    {
                        "artifact_type": "slides_package",
                        "title": "答辩 PPT",
                        "preview": {"slides": [{"title": "封面", "bullets": []}]},
                    }
                ],
            },
        )

        self.assertEqual(workflow.requirement_service.updated, ["run_sync"])
        self.assertEqual(len(workflow.feishu_artifact_integrator.synced), 1)
        self.assertEqual(workflow.feishu_artifact_integrator.synced[0].task_run_id, "run_sync")

    def test_persist_task_run_result_skips_feishu_sync_for_non_visual_artifacts(self) -> None:
        workflow = _WorkflowForPersist()
        service = WorkflowResultPersistence(workflow)

        service.persist_task_run_result(
            "run_doc",
            message_text="更新摘要",
            session_id="session_1",
            result={
                "mode": "analysis",
                "reply_preview": "已整理摘要",
                "analysis": None,
                "artifacts": [
                    {
                        "artifact_type": "note",
                        "title": "摘要",
                        "preview": {"text": "done"},
                    }
                ],
            },
        )

        self.assertEqual(workflow.requirement_service.updated, ["run_doc"])
        self.assertEqual(workflow.feishu_artifact_integrator.synced, [])


if __name__ == "__main__":
    unittest.main()
