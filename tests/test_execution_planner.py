import unittest

from app.services.execution_planner import ExecutionPlanner, RequestProtocol


class ExecutionPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ExecutionPlanner()

    def test_normalizes_legacy_aliases(self) -> None:
        self.assertEqual(self.planner.normalize_operation("show"), "read")
        self.assertEqual(self.planner.normalize_operation("share"), "deliver")
        self.assertEqual(self.planner.normalize_object("feishu_doc"), "doc")
        self.assertEqual(self.planner.normalize_object("flowchart"), "canvas")

    def test_doc_plan_can_append_requested_slides(self) -> None:
        llm_result = {"requested_outputs": ["doc", "slides"]}
        protocol = RequestProtocol(operation="create", object="doc", route="doc")

        plan = self.planner.resolve_execution_plan(
            intent="doc",
            reason="",
            instruction="整理成文档并生成 PPT",
            llm_result=llm_result,
            protocol=protocol,
        )

        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_slides"])

    def test_clarification_artifact_is_marked_needs_confirmation(self) -> None:
        plan = self.planner.build_fallback_plan(intent="doc", reason="", instruction="整理成文档", llm_result={})
        artifact = self.planner.build_plan_artifact(
            plan=plan,
            reason="需要确认",
            llm_result={
                "operation": "create",
                "object": "doc",
                "route": "doc",
                "clarification": {
                    "needed": True,
                    "question": "写给谁看？",
                    "blocking": True,
                },
            },
        )

        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["status"], "needs_confirmation")
        self.assertEqual(artifact["preview"]["clarification"]["question"], "写给谁看？")


if __name__ == "__main__":
    unittest.main()
