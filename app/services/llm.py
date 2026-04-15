import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible LLM client for collaboration extraction and drafting."""

    def __init__(self) -> None:
        self.api_key = settings.llm_api_key or settings.anthropic_auth_token
        self.base_url = (settings.llm_base_url or settings.anthropic_base_url).rstrip("/")
        self.model = settings.llm_model or settings.anthropic_model

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def extract_collaboration(self, raw_text: str) -> dict[str, Any]:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._extraction_prompt()},
                {"role": "user", "content": raw_text},
            ],
        }

        data = self._post_chat_completion(payload)
        text = self._extract_text(data)
        result = self._parse_json(text)
        logger.info("LLM extraction succeeded with %s task(s)", len(result.get("tasks", [])))
        return result

    def generate_presentation_outline(self, workspace_context: str, instruction: str) -> str:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

        payload = {
            "model": self.model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": self._presentation_prompt()},
                {
                    "role": "user",
                    "content": f"{workspace_context}\n\n[本次请求]\n{instruction}",
                },
            ],
        }

        data = self._post_chat_completion(payload)
        return self._extract_text(data).strip()

    def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def _extract_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Unexpected LLM response format: missing choices.")

        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM response did not include text content.")
        return content

    def _parse_json(self, text: str) -> dict[str, Any]:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("LLM response did not contain a JSON object.")

        return json.loads(candidate[start : end + 1])

    def _extraction_prompt(self) -> str:
        return """
你是飞书办公协作助手，负责把多人群聊讨论整理成结构化协作结果。
你必须只输出合法 JSON，不要输出 markdown，不要输出解释。

输出格式：
{
  "summary": "中文摘要",
  "tasks": [
    {
      "title": "任务标题",
      "owner": "负责人；不明确则填 TBD",
      "priority": "high|medium|low",
      "due_date": "截止时间；不明确则填 TBD",
      "status": "draft",
      "notes": "支撑该任务的原始讨论片段"
    }
  ],
  "risks": ["中文风险点"],
  "next_actions": ["中文下一步建议"]
}

规则：
- 只提取当前讨论中真正明确的任务和行动项。
- 如果内容主要是闲聊或未形成动作，请返回空 tasks 数组。
- 如果没有明确负责人，填写 TBD。
- 如果没有明确截止时间，填写 TBD。
- 所有输出都使用简体中文。
""".strip()

    def _presentation_prompt(self) -> str:
        return """
你是办公协作助手，负责把飞书群聊讨论整理成可执行的演示稿大纲。
请输出简体中文，格式清晰，适合直接复制到文档或 PPT 中。

输出要求：
- 先给一个演示主题
- 再给“适用场景”
- 然后按 5 到 7 页输出每页标题和要点
- 最后补一段“演示时重点强调”

你需要尽量利用已有的任务、风险、结论和待办信息，不要编造不存在的业务事实。
""".strip()
