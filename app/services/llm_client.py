from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OpenAICompatibleJSONClient:
    """Small OpenAI-compatible chat client that returns JSON objects."""

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def chat_json(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        data = self.post_chat_completion(payload, request_name=request_name, timeout_seconds=timeout_seconds)
        text = self.extract_text(data)
        return self.parse_json(text)

    def post_chat_completion(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        started_at = time.perf_counter()
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        logger.info(
            "LLM request completed: request=%s elapsed_ms=%.1f model=%s",
            request_name,
            (time.perf_counter() - started_at) * 1000,
            payload.get("model"),
        )
        return data

    @staticmethod
    def extract_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Unexpected LLM response format: missing choices.")

        message = choices[0].get("message", {})
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM response did not include text content.")
        return content

    @staticmethod
    def parse_json(text: str) -> dict[str, Any]:
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
