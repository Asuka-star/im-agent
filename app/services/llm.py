import json
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """Anthropic-compatible LLM client with structured extraction support."""

    def __init__(self) -> None:
        self.auth_token = settings.anthropic_auth_token
        self.base_url = settings.anthropic_base_url.rstrip("/")
        self.model = settings.anthropic_model

    def is_configured(self) -> bool:
        return bool(self.auth_token and self.base_url and self.model)

    def extract_collaboration(self, raw_text: str) -> dict[str, Any]:
        if not self.is_configured():
            raise RuntimeError("Anthropic-compatible LLM config is incomplete.")

        prompt = self._build_prompt(raw_text)
        payload = {
            "model": self.model,
            "max_tokens": 1200,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{self.base_url}/messages", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        text = self._extract_text(data)
        result = self._parse_json(text)
        logger.info("LLM extraction succeeded with %s task(s)", len(result.get("tasks", [])))
        return result

    def _extract_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content", [])
        if not isinstance(content, list):
            raise RuntimeError("Unexpected LLM response format: content is not a list.")

        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))

        text = "\n".join(part for part in text_parts if part)
        if not text:
            raise RuntimeError("LLM response did not include text output.")
        return text

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

    def _build_prompt(self, raw_text: str) -> str:
        return f"""
You are extracting structured collaboration data from a Feishu chat message.

Return only valid JSON with this schema:
{{
  "summary": "string",
  "tasks": [
    {{
      "title": "string",
      "owner": "string or TBD",
      "priority": "high|medium|low",
      "due_date": "string or TBD",
      "status": "draft",
      "notes": "string"
    }}
  ],
  "risks": ["string"],
  "next_actions": ["string"]
}}

Rules:
- Extract all concrete tasks mentioned in the message.
- If no owner is explicit, use "TBD".
- If no due date is explicit, use "TBD".
- Keep status as "draft".
- Notes should preserve the supporting clause from the source text.
- If the text is short or vague, still produce your best structured interpretation.
- Do not include markdown, explanations, or any text outside JSON.

Source text:
{raw_text}
""".strip()
