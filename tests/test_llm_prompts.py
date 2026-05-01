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
        self.assertEqual(service._analysis_request_prompt("risks"), service.prompts.analysis_request("risks"))
        self.assertEqual(service._doc_edit_intent_prompt(), service.prompts.doc_edit_intent())

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

    def test_llm_service_builds_standard_json_payload(self) -> None:
        service = LLMService()
        service.model = "demo-model"

        payload = service._json_payload(system_prompt="sys", user_content="user", temperature=0.1)

        self.assertEqual(payload["model"], "demo-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "user"})

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
