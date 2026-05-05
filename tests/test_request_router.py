import unittest

from app.services.request_router import RequestRouter


class FakeRouteLLM:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.requests: list[str] = []

    def is_configured(self) -> bool:
        return True

    def route_workspace_request(self, instruction: str) -> dict:
        self.requests.append(instruction)
        return self.result


class RequestRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = RequestRouter()

    def test_task_list_query_routes_to_status(self) -> None:
        decision = self.router.route_by_rule("查看任务列表")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "rule")

    def test_exact_task_list_command_skips_llm_router(self) -> None:
        llm = FakeRouteLLM({"route": "doc", "confidence": 1.0, "reason": "wrong"})
        decision = self.router.route("任务列表", llm_service=llm)

        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "rule_exact")
        self.assertEqual(llm.requests, [])

    def test_task_list_command_with_extra_text_does_not_use_exact_rule(self) -> None:
        llm = FakeRouteLLM({"route": "status", "confidence": 0.88, "reason": "natural language task query"})
        decision = self.router.route("任务列表吧", llm_service=llm)

        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "llm")
        self.assertEqual(llm.requests, ["任务列表吧"])

    def test_other_exact_task_command_skips_llm_router(self) -> None:
        llm = FakeRouteLLM({"route": "doc", "confidence": 1.0, "reason": "wrong"})
        decision = self.router.route("待办清单", llm_service=llm)

        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "rule_exact")
        self.assertEqual(llm.requests, [])

    def test_exact_risk_command_skips_llm_router(self) -> None:
        llm = FakeRouteLLM({"route": "doc", "confidence": 1.0, "reason": "wrong"})
        decision = self.router.route("风险清单", llm_service=llm)

        self.assertEqual(decision.route, "risks")
        self.assertEqual(decision.source, "rule_exact")
        self.assertEqual(llm.requests, [])

    def test_broad_task_query_defers_to_llm_router_when_available(self) -> None:
        llm = FakeRouteLLM({"route": "status", "confidence": 0.91, "reason": "user asks task status"})
        decision = self.router.route("现在都有什么任务", llm_service=llm)

        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "llm")
        self.assertEqual(llm.requests, ["现在都有什么任务"])

    def test_broad_task_assignment_defers_to_llm_router_when_available(self) -> None:
        llm = FakeRouteLLM({"route": "tasks", "confidence": 0.89, "reason": "user claims an existing task"})
        decision = self.router.route("统计任务李彪由我来实现", llm_service=llm)

        self.assertEqual(decision.route, "tasks")
        self.assertEqual(decision.source, "llm")
        self.assertEqual(llm.requests, ["统计任务李彪由我来实现"])

    def test_fuzzy_task_list_query_routes_to_status(self) -> None:
        decision = self.router.route_by_rule("现在都有什么任务")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "rule")

    def test_task_completion_statement_routes_to_task_update(self) -> None:
        decision = self.router.route_by_rule("张三的任务已经完成了")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "tasks")
        self.assertEqual(decision.source, "rule")

    def test_task_completion_rule_overrides_llm_status_route(self) -> None:
        llm = FakeRouteLLM({"route": "status", "confidence": 0.95, "reason": "misread as task query"})

        decision = self.router.route(
            "\u5f20\u4e09\u7684\u4efb\u52a1\u5b8c\u6210\u4e86",
            llm_service=llm,
        )

        self.assertEqual(decision.route, "tasks")
        self.assertEqual(decision.source, "rule")
        self.assertEqual(llm.requests, [])

    def test_first_person_completion_statement_routes_to_task_update(self) -> None:
        decision = self.router.route_by_rule("我已完成后端开发任务")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "tasks")
        self.assertEqual(decision.source, "rule")

    def test_unassigned_help_request_routes_to_tasks(self) -> None:
        decision = self.router.route_by_rule("需要有人来帮我完成后端开发任务")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "tasks")
        self.assertEqual(decision.source, "rule")

    def test_doc_artifact_request_routes_to_doc(self) -> None:
        decision = self.router.route_by_rule("你再来总结成文档")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc",))

    def test_compound_artifact_output_request_routes_without_local_target_clarification(self) -> None:
        decision = self.router.route_by_rule("总结一下目前的任务，然后写到文档里面")

        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.source, "rule")
        self.assertFalse(decision.needs_clarification)
        self.assertEqual(decision.requested_outputs, ("doc",))

    def test_compound_doc_and_ppt_output_request_tolerates_destination_marker(self) -> None:
        decision = self.router.route_by_rule("总结目前任务，写到文档里，并做成PPT")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertFalse(decision.needs_clarification)
        self.assertEqual(decision.requested_outputs, ("doc", "slides"))

    def test_document_section_delete_without_doc_keyword_requires_clarification(self) -> None:
        decision = self.router.route("帮我删除Update (2026-04-29 15:43)栏以下的内容")

        self.assertEqual(decision.route, "unknown")
        self.assertNotEqual(decision.source, "rule")

    def test_artifact_target_without_operation_requires_clarification(self) -> None:
        decision = self.router.route_by_rule("需要来帮我将文档里面Update (2026-04-29 15:43)栏以下的内容")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)

    def test_slide_target_without_operation_requires_clarification(self) -> None:
        decision = self.router.route_by_rule("帮我将 PPT 里面第二页的内容")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)

    def test_doc_and_presentation_request_preserves_compound_outputs(self) -> None:
        decision = self.router.route_by_rule("帮我整理成文档并生成PPT")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc", "slides"))

    def test_doc_and_canvas_request_preserves_compound_outputs(self) -> None:
        decision = self.router.route_by_rule("帮我整理成文档并画一个架构图")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc", "canvas"))

    def test_slides_and_canvas_request_preserves_compound_outputs(self) -> None:
        decision = self.router.route_by_rule("生成PPT并画流程图")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides", "canvas"))

    def test_doc_slides_and_canvas_request_preserves_all_outputs(self) -> None:
        decision = self.router.route_by_rule("帮我整理成文档、生成PPT并画流程图")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc", "slides", "canvas"))

    def test_compound_outputs_follow_user_mentioned_order(self) -> None:
        decision = self.router.route_by_rule("先生成PPT，再整理成文档")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides", "doc"))

    def test_canvas_can_be_primary_when_mentioned_first(self) -> None:
        decision = self.router.route_by_rule("先画流程图，再生成PPT")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "canvas")
        self.assertEqual(decision.requested_outputs, ("canvas", "slides"))

    def test_presentation_only_request_with_doc_exclusion_routes_to_slides(self) -> None:
        decision = self.router.route_by_rule("只生成PPT，不要写文档")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides",))

    def test_task_organize_request_routes_to_tasks(self) -> None:
        decision = self.router.route_by_rule("帮我整理任务清单")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "tasks")

    def test_vague_request_asks_for_clarification(self) -> None:
        decision = self.router.route_by_rule("帮我整理一下")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)

    def test_ambiguous_handling_question_asks_for_clarification(self) -> None:
        decision = self.router.route_by_rule("你看这个怎么处理")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "unknown")
        self.assertTrue(decision.needs_clarification)

    def test_presentation_outline_routes_to_slides(self) -> None:
        decision = self.router.route_by_rule("生成演示提纲")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "slides")
        self.assertEqual(decision.requested_outputs, ("slides",))

    def test_report_material_routes_to_slides(self) -> None:
        decision = self.router.route_by_rule("整理成汇报材料，五页左右")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "slides")

    def test_canvas_request_routes_to_canvas(self) -> None:
        decision = self.router.route_by_rule("draw an architecture diagram on a canvas")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "canvas")
        self.assertEqual(decision.requested_outputs, ("canvas",))

    def test_chinese_canvas_request_routes_to_canvas(self) -> None:
        decision = self.router.route_by_rule("帮我画一张系统架构图")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "canvas")
        self.assertEqual(decision.requested_outputs, ("canvas",))

    def test_llm_route_result_is_normalized(self) -> None:
        decision = self.router.route_from_llm_result(
            {"route": "document", "confidence": 1.5, "reason": "用户要写成文档"}
        )

        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.source, "llm")
        self.assertEqual(decision.confidence, 1.0)
        self.assertEqual(decision.requested_outputs, ("doc",))

    def test_low_confidence_llm_route_asks_for_clarification(self) -> None:
        decision = self.router.route_from_llm_result(
            {"route": "summary", "confidence": 0.3, "reason": "不确定用户要什么产物"}
        )

        self.assertEqual(decision.route, "summary")
        self.assertTrue(decision.needs_clarification)


if __name__ == "__main__":
    unittest.main()
