import unittest

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

    def test_llm_client_parses_fenced_json(self) -> None:
        parsed = OpenAICompatibleJSONClient.parse_json('```json\n{"ok": true}\n```')

        self.assertEqual(parsed, {"ok": True})

    def test_llm_client_rejects_missing_text(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing choices"):
            OpenAICompatibleJSONClient.extract_text({"choices": []})


if __name__ == "__main__":
    unittest.main()
