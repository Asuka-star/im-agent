import unittest

from app.services.request_router import RequestRouter


class RequestRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = RequestRouter()

    def test_task_list_query_routes_to_status(self) -> None:
        decision = self.router.route_by_rule("查看任务列表")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "status")
        self.assertEqual(decision.source, "rule")

    def test_doc_artifact_request_routes_to_doc(self) -> None:
        decision = self.router.route_by_rule("你再来总结成文档")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc",))

    def test_doc_and_presentation_request_preserves_compound_outputs(self) -> None:
        decision = self.router.route_by_rule("帮我整理成文档并生成PPT")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.route, "doc")
        self.assertEqual(decision.requested_outputs, ("doc", "slides"))

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
