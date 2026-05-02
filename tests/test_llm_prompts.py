import unittest
from unittest.mock import patch

from app.services.llm_client import OpenAICompatibleJSONClient
from app.services.llm import LLMService
from app.services.llm_prompts import LLMPromptBuilder


class LLMPromptTests(unittest.TestCase):
    def test_workspace_prompt_is_compact_and_canvas_aware(self) -> None:
        prompt = LLMPromptBuilder().workspace_request()

        self.assertLess(len(prompt), 5000)
        self.assertIn("generate_canvas", prompt)
        self.assertIn("Return valid JSON only", prompt)
        self.assertIn("Simplified Chinese", prompt)

    def test_prompts_do_not_contain_common_mojibake_markers(self) -> None:
        prompts = [
            LLMPromptBuilder().route(),
            LLMPromptBuilder().doc_request(),
            LLMPromptBuilder().analysis_request("tasks"),
            LLMPromptBuilder().presentation(),
        ]

        for prompt in prompts:
            self.assertNotIn("�", prompt)
            self.assertNotIn("鈥", prompt)
            self.assertNotIn("鍙", prompt)

    def test_llm_service_keeps_prompt_wrapper_methods(self) -> None:
        service = LLMService()

        self.assertEqual(service._route_prompt(), service.prompts.route())
        self.assertEqual(service._dag_plan_prompt(), service.prompts.dag_plan())
        self.assertEqual(service._analysis_request_prompt("risks"), service.prompts.analysis_request("risks"))
        self.assertEqual(service._doc_edit_intent_prompt(), service.prompts.doc_edit_intent())
        self.assertEqual(service._next_action_rerank_prompt(), service.prompts.next_action_rerank())

    def test_dag_plan_prompt_is_planning_only_and_bounded(self) -> None:
        prompt = LLMPromptBuilder().dag_plan()

        self.assertLess(len(prompt), 3500)
        self.assertIn("lightweight DAG planner", prompt)
        self.assertIn("Do not draft document text", prompt)
        self.assertIn("sync_doc|generate_slides|generate_canvas", prompt)
        self.assertIn("requested_outputs", prompt)
        self.assertIn("Do not include content payload keys", prompt)

    def test_doc_edit_intent_prompt_is_contract_only(self) -> None:
        prompt = LLMPromptBuilder().doc_edit_intent()

        self.assertIn("Only parse the user's requested document mutation", prompt)
        self.assertIn("Do not draft document content", prompt)
        self.assertIn('"artifact_edit_plan"', prompt)
        self.assertIn('"include_anchor"', prompt)
        self.assertIn('"confirmation"', prompt)
        self.assertNotIn('"doc":{"title"', prompt)

    def test_route_prompt_describes_compound_artifact_requests(self) -> None:
        prompt = LLMPromptBuilder().route()

        self.assertIn("Compound requests", prompt)
        self.assertIn("analysis intent", prompt)
        self.assertIn("requested_outputs", prompt)
        self.assertIn("Do not include content payload keys", prompt)

    def test_next_action_rerank_prompt_is_dedicated_and_bounded(self) -> None:
        prompt = LLMPromptBuilder().next_action_rerank()

        self.assertIn("rule-generated candidates", prompt)
        self.assertIn("Do not invent new actions", prompt)
        self.assertIn('"order"', prompt)
        self.assertIn("action_id values already present", prompt)
        self.assertNotIn('"plan"', prompt)

    def test_llm_service_builds_standard_json_payload(self) -> None:
        service = LLMService()
        service.model = "demo-model"

        payload = service._json_payload(system_prompt="sys", user_content="user", temperature=0.1)

        self.assertEqual(payload["model"], "demo-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "user"})

    def test_plan_workspace_request_uses_lightweight_timeout_and_cache(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        result = {
            "operation": "create",
            "object": "slides",
            "confidence": 0.82,
            "requested_outputs": ["slides", "canvas"],
            "plan": {"steps": [{"id": "step_1", "type": "generate_slides"}]},
        }

        with patch.object(service, "_chat_json", return_value=result) as chat_json:
            first = service.plan_workspace_request("[workspace]", "做汇报材料并画流程图")
            first["object"] = "workspace"
            first["requested_outputs"].append("doc")
            second = service.plan_workspace_request("[workspace]", "做汇报材料并画流程图")

        self.assertEqual(first["object"], "workspace")
        self.assertEqual(second["object"], "slides")
        self.assertEqual(second["requested_outputs"], ["slides", "canvas"])
        chat_json.assert_called_once()
        self.assertEqual(chat_json.call_args.kwargs["request_name"], "plan_workspace_request")

    def test_lightweight_plan_strips_generated_content_payloads(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        result = {
            "action": "create",
            "target": "slides",
            "confidence": 0.82,
            "requested_outputs": ["presentation", "diagram", "slides", "deck"],
            "plan": {
                "goal": "生成汇报",
                "steps": [
                    {
                        "id": "step_1",
                        "type": "presentation",
                        "title": "生成汇报材料",
                        "depends_on": [None, "", "step_0"],
                        "payload": {"slides": ["should be dropped"]},
                    },
                    {"id": "step_2", "type": "write_full_deck", "title": "非法步骤"},
                ],
            },
            "slides": {"slides": [{"title": "不该被轻量规划结果携带"}]},
            "doc": {"title": "不该被轻量规划结果携带"},
            "tasks": [{"title": "不该被轻量规划结果携带"}],
            "summary": "不该被轻量规划结果携带",
            "next_actions": ["不该被轻量规划结果携带"],
        }

        with patch.object(service, "_chat_json", return_value=result):
            planned = service.plan_workspace_request("[workspace]", "生成汇报材料")

        self.assertEqual(planned["requested_outputs"], ["slides", "canvas"])
        self.assertEqual(planned["operation"], "create")
        self.assertEqual(planned["object"], "slides")
        self.assertEqual(planned["plan"]["steps"], [
            {
                "id": "step_1",
                "type": "generate_slides",
                "title": "生成汇报材料",
                "depends_on": [],
            }
        ])
        self.assertNotIn("slides", planned)
        self.assertNotIn("doc", planned)
        self.assertNotIn("tasks", planned)
        self.assertNotIn("summary", planned)
        self.assertNotIn("next_actions", planned)

    def test_lightweight_route_strips_non_route_payloads(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "route": "presentation",
                "confidence": 0.91,
                "requested_outputs": ["presentation", "feishu_doc"],
                "reason": "用户需要汇报材料",
                "slides": {"slides": [{"title": "不该保留"}]},
            },
        ):
            routed = service.route_workspace_request("生成项目汇报 PPT")

        self.assertEqual(routed["requested_outputs"], ["slides", "doc"])
        self.assertNotIn("slides", routed)

    def test_lightweight_sanitizers_tolerate_scalar_outputs_and_dropped_dependencies(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "operation": "create",
                "object": "canvas",
                "requested_outputs": "flowchart",
                "plan": {
                    "steps": [
                        {"id": "step_1", "type": "generate_canvas", "depends_on": ["step_2", "step_1", "missing"]},
                        {"id": "step_2", "type": "write_full_deck"},
                    ]
                },
            },
        ):
            planned = service.plan_workspace_request("[workspace]", "画流程图")

        self.assertEqual(planned["requested_outputs"], ["canvas"])
        self.assertEqual(planned["plan"]["steps"], [
            {"id": "step_1", "type": "generate_canvas", "title": "", "depends_on": []}
        ])

    def test_llm_service_context_wrapper_uses_clean_chinese(self) -> None:
        content = LLMService._context_request_content("上下文", "生成文档")

        self.assertIn("[工作区上下文]", content)
        self.assertIn("[当前请求]", content)
        self.assertNotIn("ç", content)

    def test_doc_edit_intent_heuristic_detects_range_mutations(self) -> None:
        self.assertTrue(
            LLMService._instruction_needs_doc_edit_intent(
                "将文档里“后续计划”后面的内容全部删除",
                "[当前协作文档]\n后续计划\nUpdate",
            )
        )
        self.assertFalse(LLMService._instruction_needs_doc_edit_intent("帮我总结一下最近任务", ""))

    def test_resolve_doc_request_merges_dedicated_edit_plan(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        edit_plan = {
            "artifact_type": "doc",
            "mutation_required": True,
            "scope": "targeted",
            "ops": [
                {
                    "type": "delete",
                    "target": {"kind": "heading", "query": "后续计划", "scope": "after", "include_anchor": False},
                    "payload": {},
                    "reason": "删除锚点后的内容",
                }
            ],
        }

        with patch.object(service, "resolve_doc_edit_intent", return_value={"reason": "解析编辑意图", "artifact_edit_plan": edit_plan}):
            with patch.object(service, "_chat_json", return_value={"doc": {"title": "项目分工", "sections": []}}):
                result = service.resolve_doc_request("[当前协作文档]\n后续计划", "将文档里“后续计划”后面的内容全部删除")

        self.assertIs(result["artifact_edit_plan"], edit_plan)
        self.assertEqual(result["route"], "doc")
        self.assertEqual(result["object"], "doc")

    def test_resolve_doc_request_skips_edit_intent_for_plain_draft(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(service, "resolve_doc_edit_intent") as edit_intent:
            with patch.object(service, "_chat_json", return_value={"doc": {"title": "项目分工", "sections": []}}):
                service.resolve_doc_request("近期任务", "帮我总结一下最近任务并写成文档")

        edit_intent.assert_not_called()

    def test_llm_client_parses_fenced_json(self) -> None:
        parsed = OpenAICompatibleJSONClient.parse_json('```json\n{"ok": true}\n```')

        self.assertEqual(parsed, {"ok": True})

    def test_llm_client_rejects_missing_text(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing choices"):
            OpenAICompatibleJSONClient.extract_text({"choices": []})


if __name__ == "__main__":
    unittest.main()
