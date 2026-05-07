import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api.routes.task_runs import confirm_task_run, revise_task_run_canvas
from app.schemas.task_run import CanvasRevisionRequest, ConfirmationAnswerRequest


class TaskRunRouteTests(unittest.TestCase):
    def test_confirm_route_forwards_override_instruction(self) -> None:
        confirmation_record = SimpleNamespace(
            task_run_id="run_offline_1",
            confirmation_id="confirm_1",
            status="answered",
            answer_value="更新文档 + 当前 PPT",
            already_answered=False,
        )
        with patch(
            "app.api.routes.task_runs.task_run_service.resolve_confirmation",
            return_value=confirmation_record,
        ) as resolve_mock:
            with patch(
                "app.api.routes.task_runs.workflow_service.resume_task_run_after_confirmation",
                return_value={"task_run_id": "run_offline_1"},
            ) as resume_mock:
                result = asyncio.run(
                    confirm_task_run(
                        "run_offline_1",
                        ConfirmationAnswerRequest(
                            confirmation_id="confirm_1",
                            answer_value="更新文档 + 当前 PPT",
                            answered_by="tester",
                            override_instruction="只同步第 3 页",
                        ),
                    )
                )

        self.assertEqual(result.confirmation_id, "confirm_1")
        resolve_mock.assert_called_once_with(
            "run_offline_1",
            confirmation_id="confirm_1",
            answer_value="更新文档 + 当前 PPT",
            answered_by="tester",
        )
        resume_mock.assert_called_once_with(
            "run_offline_1",
            confirmation_id="confirm_1",
            answer_value="更新文档 + 当前 PPT",
            answered_by="tester",
            override_instruction="只同步第 3 页",
        )

    def test_revise_canvas_route_returns_detail(self) -> None:
        fake_detail = SimpleNamespace(task_run_id="run_canvas_1")
        with patch(
            "app.api.routes.task_runs.workflow_service.revise_canvas_from_task_run",
            return_value=fake_detail,
        ) as mocked:
            result = asyncio.run(
                revise_task_run_canvas(
                    "run_canvas_1",
                    CanvasRevisionRequest(
                        instruction="把风险节点移动到右侧",
                        requested_by="tester",
                        artifact_id="artifact_canvas_1",
                    ),
                )
            )

        self.assertEqual(result.task_run_id, "run_canvas_1")
        mocked.assert_called_once_with(
            "run_canvas_1",
            instruction="把风险节点移动到右侧",
            requested_by="tester",
            artifact_id="artifact_canvas_1",
        )

    def test_revise_canvas_route_translates_validation_error(self) -> None:
        with patch(
            "app.api.routes.task_runs.workflow_service.revise_canvas_from_task_run",
            side_effect=ValueError("No canvas artifact found for this task run."),
        ):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(
                    revise_task_run_canvas(
                        "run_canvas_missing",
                        CanvasRevisionRequest(instruction="更新画布"),
                    )
                )

        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
