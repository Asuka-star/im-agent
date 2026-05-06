import unittest

from app.schemas.task import TaskItem
from app.services.graph.runner import GraphRunner
from app.services.request_router import RequestRouter
from tests.graph.test_graph_runner import ExecutableFakeWorkflow, FakeLLMService, message


class FakeRouteLLM:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.requests: list[str] = []

    def is_configured(self) -> bool:
        return True

    def route_workspace_request(self, instruction: str) -> dict:
        self.requests.append(instruction)
        return dict(self.result)


class MultiTaskWorkflow(ExecutableFakeWorkflow):
    def _context_tasks_for_message(self, message):
        return [
            TaskItem(
                title="整理项目进展",
                owner="张三",
                priority="medium",
                due_date="TBD",
                status="draft",
                notes="",
            ),
            TaskItem(
                title="生成汇报 PPT",
                owner="李四",
                priority="medium",
                due_date="TBD",
                status="draft",
                notes="",
            ),
        ]


class RequestRouterDirtyInputGuardrailTests(unittest.TestCase):
    def test_low_confidence_llm_route_requires_clarification(self) -> None:
        llm = FakeRouteLLM(
            {
                "route": "doc",
                "confidence": 0.21,
                "reason": "user input is noisy and lacks a stable target",
            }
        )

        decision = RequestRouter().route("随便弄一下，文档 PPT 那些你看着办？？？", llm_service=llm)

        self.assertEqual(decision.source, "llm")
        self.assertEqual(decision.route, "doc")
        self.assertTrue(decision.needs_clarification)
        self.assertEqual(decision.requested_outputs, ())
        self.assertEqual(llm.requests, ["随便弄一下，文档 PPT 那些你看着办？？？"])

    def test_unknown_llm_route_requires_clarification_without_rule_augmentation(self) -> None:
        llm = FakeRouteLLM(
            {
                "route": "unknown",
                "confidence": 0.83,
                "reason": "message mentions artifacts but does not ask for an action",
            }
        )

        decision = RequestRouter().route("我们先聊聊文档和 PPT 的方向，别急着动", llm_service=llm)

        self.assertEqual(decision.source, "llm")
        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)
        self.assertEqual(decision.requested_outputs, ())

    def test_vague_local_fallback_requires_clarification(self) -> None:
        decision = RequestRouter().route("帮我处理一下")

        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)
        self.assertLess(decision.confidence, 0.5)


class GraphDirtyInputGuardrailTests(unittest.TestCase):
    def test_low_confidence_generate_command_pauses_before_workers(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "doc",
                    "operation": "generate",
                    "object": "workspace",
                    "confidence": 0.32,
                    "reason": "noisy command with no concrete output target",
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("随便弄一下"),
            task_run_id="run_dirty_low_confidence",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["task_run_status"], "waiting_confirmation")
        self.assertEqual(workflow.doc_execution.calls, [])
        self.assertEqual(workflow.slides_execution.calls, [])
        self.assertEqual(workflow.canvas_execution.calls, [])
        self.assertEqual(workflow.result_persistence.persisted, [])

    def test_ambiguous_task_delete_pauses_before_task_mutation(self) -> None:
        workflow = MultiTaskWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "tasks",
                    "operation": "remove",
                    "object": "task",
                    "target_text": "",
                    "destructive": True,
                    "confidence": 0.94,
                    "reason": "delete request does not identify a unique task",
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("把那个任务删掉"),
            task_run_id="run_dirty_delete",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(workflow.memory_service.saved_rounds, [])
        self.assertEqual(workflow.result_persistence.persisted, [])
        metadata = workflow.task_run_service.get_task_run_metadata("run_dirty_delete")
        resume_payload = metadata.get("resume_after_graph_confirmation")
        self.assertIsInstance(resume_payload, dict)
        state_payload = resume_payload.get("state")
        review = state_payload.get("review") if isinstance(state_payload, dict) else {}
        clarification = review.get("clarification") if isinstance(review, dict) else {}
        self.assertEqual(len(clarification.get("candidates") or []), 2)

    def test_seeded_llm_clarification_pauses_without_execution(self) -> None:
        workflow = ExecutableFakeWorkflow(
            FakeLLMService(
                {
                    "mode": "workspace_action",
                    "route": "unknown",
                    "operation": "clarify",
                    "object": "unknown",
                    "confidence": 0.86,
                    "needs_clarification": True,
                    "reason": "user intent is not actionable",
                    "clarification": {
                        "question": "你想让我总结、生成产物，还是修改已有内容？",
                        "reason": "缺少可执行动作",
                        "options": ["总结", "生成文档", "修改已有内容"],
                        "blocking": True,
                    },
                }
            )
        )

        result = GraphRunner(workflow).run_task_graph(
            message("嗯就那个吧"),
            task_run_id="run_dirty_seeded_clarification",
            workspace_context="workspace",
            active_episode_id=None,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["pending_confirmation"])
        self.assertIn("总结", result["reply_preview"])
        self.assertEqual(workflow.doc_execution.calls, [])
        self.assertEqual(workflow.slides_execution.calls, [])
        self.assertEqual(workflow.canvas_execution.calls, [])


if __name__ == "__main__":
    unittest.main()
