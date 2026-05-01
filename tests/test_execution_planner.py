import unittest

from app.services.document_package_builder import DocumentPackageBuilder
from app.services.execution_planner import ExecutionPlanner, RequestProtocol


class ExecutionPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = ExecutionPlanner()

    def test_normalizes_legacy_aliases(self) -> None:
        self.assertEqual(self.planner.normalize_operation("show"), "read")
        self.assertEqual(self.planner.normalize_operation("share"), "deliver")
        self.assertEqual(self.planner.normalize_operation("delete"), "update")
        self.assertEqual(self.planner.normalize_operation("remove"), "update")
        self.assertEqual(self.planner.normalize_object("feishu_doc"), "doc")
        self.assertEqual(self.planner.normalize_object("flowchart"), "canvas")

    def test_delete_doc_protocol_still_executes_doc_sync(self) -> None:
        llm_result = {
            "operation": "delete",
            "object": "doc",
            "route": "doc",
            "doc": {"title": "Doc", "sections": [{"heading": "Summary", "paragraphs": ["Keep"]}]},
        }

        plan = self.planner.resolve_execution_plan(
            intent="doc",
            reason="",
            instruction="delete one doc section",
            llm_result=llm_result,
        )

        self.assertEqual(llm_result["operation"], "update")
        self.assertEqual(llm_result["object"], "doc")
        self.assertEqual(llm_result["route"], "doc")
        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc"])

    def test_document_package_accepts_nested_edit_plan(self) -> None:
        package = DocumentPackageBuilder().package_from_llm_result(
            instruction="delete old section",
            llm_result={
                "doc": {
                    "title": "Doc",
                    "sections": [{"heading": "Summary", "paragraphs": ["Keep"]}],
                    "artifact_edit_plan": {
                        "artifact_type": "doc",
                        "operations": [{"type": "delete", "target": {"queries": ["Old"]}}],
                    },
                }
            },
        )

        self.assertIsNotNone(package)
        self.assertEqual(package["artifact_edit_plan"]["operations"][0]["type"], "delete")

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

    def test_doc_plan_can_append_requested_canvas(self) -> None:
        llm_result = {"requested_outputs": ["doc", "canvas"]}
        protocol = RequestProtocol(operation="create", object="doc", route="doc")

        plan = self.planner.resolve_execution_plan(
            intent="doc",
            reason="",
            instruction="整理成文档并画架构图",
            llm_result=llm_result,
            protocol=protocol,
        )

        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_canvas"])

    def test_doc_plan_can_append_requested_slides_and_canvas(self) -> None:
        llm_result = {"requested_outputs": ["doc", "slides", "canvas"]}
        protocol = RequestProtocol(operation="create", object="doc", route="doc")

        plan = self.planner.resolve_execution_plan(
            intent="doc",
            reason="",
            instruction="整理成文档、生成 PPT 并画流程图",
            llm_result=llm_result,
            protocol=protocol,
        )

        self.assertEqual([step.step_type for step in plan.steps], ["sync_doc", "generate_slides", "generate_canvas"])

    def test_slides_plan_can_append_requested_canvas(self) -> None:
        llm_result = {"requested_outputs": ["slides", "canvas"]}
        protocol = RequestProtocol(operation="create", object="slides", route="slides")

        plan = self.planner.resolve_execution_plan(
            intent="slides",
            reason="",
            instruction="生成 PPT 并画流程图",
            llm_result=llm_result,
            protocol=protocol,
        )

        self.assertEqual([step.step_type for step in plan.steps], ["generate_slides", "generate_canvas"])

    def test_read_status_protocol_is_not_upgraded_by_stale_requested_outputs(self) -> None:
        llm_result = {"requested_outputs": ["doc"]}
        protocol = RequestProtocol(operation="read", object="tasks", route="status")

        plan = self.planner.resolve_execution_plan(
            intent="status",
            reason="用户只是在查询任务",
            instruction="你说说看都有什么任务",
            llm_result=llm_result,
            protocol=protocol,
        )

        self.assertEqual(llm_result["operation"], "read")
        self.assertEqual(llm_result["object"], "tasks")
        self.assertEqual(llm_result["route"], "status")
        self.assertEqual([step.step_type for step in plan.steps], ["answer_status"])

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
