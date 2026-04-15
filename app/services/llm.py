import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible client for collaboration extraction and slide drafting."""

    def __init__(self) -> None:
        self.api_key = settings.llm_api_key or settings.anthropic_auth_token
        self.base_url = (settings.llm_base_url or settings.anthropic_base_url).rstrip("/")
        self.model = settings.llm_model or settings.anthropic_model

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def extract_collaboration(self, raw_text: str) -> dict[str, Any]:
        self._ensure_configured()

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

    def generate_presentation_package(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()

        payload = {
            "model": self.model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._presentation_prompt()},
                {
                    "role": "user",
                    "content": f"{workspace_context}\n\n[Current request]\n{instruction}",
                },
            ],
        }

        data = self._post_chat_completion(payload)
        text = self._extract_text(data)
        return self._parse_json(text)

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

    def _post_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
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
You are a Feishu collaboration assistant.
Convert a multi-person chat discussion into structured collaboration output.
Return valid JSON only. No markdown, no explanation.

Schema:
{
  "summary": "Simplified Chinese summary",
  "tasks": [
    {
      "title": "task title",
      "owner": "owner or TBD",
      "priority": "high|medium|low",
      "due_date": "deadline or TBD",
      "status": "draft",
      "notes": "supporting discussion snippet"
    }
  ],
  "risks": ["risk in Simplified Chinese"],
  "next_actions": ["next action in Simplified Chinese"]
}

Rules:
- Extract only explicit actions and commitments.
- If the message is mostly discussion without clear actions, return an empty tasks array.
- If owner is unclear, use TBD.
- If due date is unclear, use TBD.
- All output must be Simplified Chinese.
""".strip()

    def _presentation_prompt(self) -> str:
        return """
You are a workplace collaboration assistant.
Turn Feishu group discussion, tasks, risks, and conclusions into a presentation draft package.
Return valid JSON only. No markdown, no explanation.
All output must be Simplified Chinese.

Schema:
{
  "theme": "presentation theme",
  "audience": "target audience or applicable scenario",
  "slides": [
    {
      "title": "slide title",
      "bullets": ["bullet 1", "bullet 2", "bullet 3"]
    }
  ],
  "emphasis": ["important speaking point 1", "important speaking point 2"],
  "assets": ["supporting material 1", "supporting material 2"]
}

Rules:
- Produce 5 to 7 slides.
- Each slide should have 2 to 4 concise bullets.
- Use only information supported by the workspace context.
- Prioritize actionability: goals, decisions, task split, timeline, risks, next steps.
""".strip()
