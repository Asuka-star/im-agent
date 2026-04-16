import json
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services.due_date import current_local_date

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible client for collaboration analysis and response planning."""

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
        result = self._chat_json(payload)
        logger.info("LLM extraction succeeded with %s task(s)", len(result.get("tasks", [])))
        return result

    def classify_intent(self, user_text: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._intent_prompt()},
                {"role": "user", "content": user_text},
            ],
        }
        result = self._chat_json(payload)
        logger.info(
            "LLM intent classification succeeded: intent=%s confidence=%s",
            result.get("intent"),
            result.get("confidence"),
        )
        return result

    def resolve_workspace_request(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._workspace_request_prompt()},
                {
                    "role": "user",
                    "content": f"[工作区上下文]\n{workspace_context}\n\n[当前请求]\n{instruction}",
                },
            ],
        }
        result = self._chat_json(payload)
        logger.info(
            "LLM workspace request resolved: intent=%s tasks=%s",
            result.get("intent"),
            len(result.get("tasks", [])) if isinstance(result.get("tasks"), list) else 0,
        )
        return result

    def should_recall_memories(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._memory_gate_prompt()},
                {
                    "role": "user",
                    "content": f"[轻量上下文]\n{workspace_context}\n\n[当前请求]\n{instruction}",
                },
            ],
        }
        result = self._chat_json(payload)
        logger.info(
            "LLM memory gate resolved: should_recall=%s confidence=%s",
            result.get("should_recall"),
            result.get("confidence"),
        )
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
                    "content": f"[工作区上下文]\n{workspace_context}\n\n[当前请求]\n{instruction}",
                },
            ],
        }
        return self._chat_json(payload)

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

    def _chat_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._post_chat_completion(payload)
        text = self._extract_text(data)
        return self._parse_json(text)

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
        today = current_local_date().isoformat()
        return f"""
You are a Feishu collaboration assistant.
Convert a multi-person chat discussion into structured collaboration output.
Return valid JSON only. No markdown, no explanation.

Schema:
{{
  "summary": "Simplified Chinese summary",
  "tasks": [
    {{
      "title": "task title",
      "owner": "owner or TBD",
      "priority": "high|medium|low",
      "due_date": "deadline or TBD",
      "status": "draft",
      "notes": "supporting discussion snippet"
    }}
  ],
  "risks": ["risk in Simplified Chinese"],
  "next_actions": ["next action in Simplified Chinese"]
}}

Rules:
- Extract only explicit actions and commitments.
- The discussion may contain structured hints such as "发言人" and "提及". If one participant assigns work to an @mentioned teammate, prefer the mentioned teammate as the owner.
- If the message is mostly discussion without clear actions, return an empty tasks array.
- If owner is unclear, use TBD.
- If due date is unclear, use TBD.
- Today is {today} in Asia/Shanghai.
- If the source mentions relative time such as 今天、明天、后天、周五前、这周二、下周三前, convert it to an absolute date in YYYY-MM-DD format when possible.
- Do not invent old years such as 2024 when the discussion uses relative dates.
- All output must be Simplified Chinese.
""".strip()

    def _workspace_request_prompt(self) -> str:
        today = current_local_date().isoformat()
        return f"""
You are the main reasoning engine of a Feishu collaboration bot.
You receive the whole recent discussion context, current task snapshot, recent summaries, and the user's latest @bot instruction.
You must look at the full context first, then decide what the user wants, and return one structured JSON response.

Return valid JSON only. No markdown, no explanation.

Schema:
{{
  "intent": "summary|tasks|risks|status|slides|bitable|help|unknown",
  "reason": "short reason in Simplified Chinese",
  "summary": "overall summary in Simplified Chinese",
  "tasks": [
    {{
      "title": "task title",
      "owner": "owner or TBD",
      "priority": "high|medium|low",
      "due_date": "YYYY-MM-DD or TBD",
      "status": "draft",
      "notes": "supporting discussion snippet"
    }}
  ],
  "risks": ["risk in Simplified Chinese"],
  "next_actions": ["next action in Simplified Chinese"],
  "status_answer": "direct answer for status-style questions",
  "slides": {{
    "theme": "presentation theme",
    "audience": "target audience or applicable scenario",
    "slides": [
      {{
        "title": "slide title",
        "bullets": ["bullet 1", "bullet 2"]
      }}
    ],
    "emphasis": ["important point 1"],
    "assets": ["supporting asset 1"]
  }}
}}

Rules:
- Today is {today} in Asia/Shanghai.
- Prefer understanding the whole discussion instead of keyword matching.
- Recent discussion lines may include structured fields like "发言人" and "提及". Treat "提及" as a strong assignee hint in multi-person collaboration.
- Distinguish clearly between the speaker, the mentioned teammate, and the final owner of a task.
- When one teammate assigns work to an @mentioned teammate, prefer the mentioned teammate as the task owner unless the discussion clearly says otherwise.
- If the latest discussion corrects or revises an earlier assignment, return the refreshed final task state instead of keeping both versions.
- For summary/tasks/risks/bitable, return the current full task list after considering revisions.
- For status, put the natural-language answer into status_answer. You may also return tasks if useful.
- For slides, fill the slides object with 5 to 7 slides and concise bullets.
- For bitable, return intent=bitable and include the tasks that should be synced.
- If the request is too vague, return intent=help.
- If the request cannot be safely understood, return intent=unknown.
- Convert relative dates like 今天、明天、这周五、下周三前 into absolute YYYY-MM-DD dates whenever possible.
- Never invent stale years such as 2024 for relative deadlines.
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

    def _intent_prompt(self) -> str:
        return """
You are an intent router for a Feishu collaboration bot.
Classify the user's request into exactly one intent.
Return valid JSON only. No markdown, no explanation.

Available intents:
- summary: summarize recent discussion
- tasks: organize TODOs / action items
- risks: identify risks / blockers
- status: answer current project/task status questions
- slides: create a presentation / report / PPT outline
- bitable: sync action items into a Feishu Bitable / task table
- help: user asks what the bot can do, or the request is too vague and needs guidance
- unknown: the request is too ambiguous to safely execute

Schema:
{
  "intent": "summary|tasks|risks|status|slides|bitable|help|unknown",
  "confidence": 0.0,
  "reason": "short reason in Simplified Chinese"
}

Rules:
- If the user asks for report outline / presentation / PPT / slides, choose slides.
- If the user asks to sync / write / update a table or Bitable, choose bitable.
- If the user asks who owns tasks / what is pending / deadlines / progress, choose status.
- If the user only says vague things like 'help me handle this' without enough detail, choose help.
- All reasons must be in Simplified Chinese.
""".strip()

    def _memory_gate_prompt(self) -> str:
        return """
You decide whether the bot needs semantic recall from long-term memory before answering.
You will receive:
- a lightweight collaboration context containing recent discussion, current task snapshot, and recent summaries
- the user's latest @bot request

Return valid JSON only. No markdown, no explanation.

Schema:
{
  "should_recall": true,
  "confidence": 0.0,
  "reason": "short reason in Simplified Chinese"
}

Rules:
- should_recall=true only when the request likely depends on older historical memory, prior decisions, change reasons, or context not guaranteed to exist in the recent discussion and task snapshot.
- should_recall=false when recent discussion + current tasks + recent summaries are already enough to answer.
- Requests such as ordinary summary, TODO extraction, current risks, syncing current tasks, or generating a report outline from the latest discussion usually do not need long-term recall.
- Requests asking why something changed, what was discussed earlier, previous decisions, historical adjustments, or comparing current status with older context usually need recall.
- All reasons must be in Simplified Chinese.
""".strip()
