import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible LLM client for structured collaboration extraction."""

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
            "messages": [
                {
                    "role": "system",
                    "content": self._system_prompt(),
                },
                {
                    "role": "user",
                    "content": raw_text,
                },
            ],
        }

        # Kimi Code docs recommend OpenAI-compatible "legacy" chat format for third-party coding agents.
        if not self._uses_kimi_coding():
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        text = self._extract_text(data)
        result = self._parse_json(text)
        logger.info("LLM extraction succeeded with %s task(s)", len(result.get("tasks", [])))
        return result

    def _uses_kimi_coding(self) -> bool:
        return "api.kimi.com/coding/v1" in self.base_url

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

        json_text = candidate[start : end + 1]
        return json.loads(json_text)

    def _system_prompt(self) -> str:
        return """
你是一个飞书群聊协作助手，负责从聊天内容中提取结构化协作信息。

你必须只输出合法 JSON，不要输出 markdown，不要输出解释。

输出格式：
{
  "summary": "中文摘要",
  "tasks": [
    {
      "title": "任务标题",
      "owner": "负责人，若不明确则填 TBD",
      "priority": "high|medium|low",
      "due_date": "截止时间，若不明确则填 TBD",
      "status": "draft",
      "notes": "支撑该任务的原始语句"
    }
  ],
  "risks": ["中文风险点"],
  "next_actions": ["中文下一步建议"]
}

规则：
- 尽量提取所有明确任务。
- 如果没有明确负责人，填写 TBD。
- 如果没有明确截止时间，填写 TBD。
- 所有输出都使用简体中文。
- 如果原文很短，也要尽力给出合理结构化结果。
""".strip()
