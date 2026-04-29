import json
import logging
import time
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
        result = self._chat_json(payload, request_name="extract_collaboration", timeout_seconds=settings.llm_timeout_seconds)
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
        result = self._chat_json(payload, request_name="classify_intent", timeout_seconds=settings.llm_timeout_seconds)
        logger.info(
            "LLM intent classification succeeded: intent=%s confidence=%s",
            result.get("intent"),
            result.get("confidence"),
        )
        return result

    def route_workspace_request(self, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._route_prompt()},
                {"role": "user", "content": instruction},
            ],
        }
        result = self._chat_json(
            payload,
            request_name="route_workspace_request",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
        logger.info(
            "LLM lightweight route resolved: route=%s confidence=%s clarification=%s",
            result.get("route"),
            result.get("confidence"),
            result.get("needs_clarification"),
        )
        return result

    def resolve_doc_request(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._doc_request_prompt()},
                {
                    "role": "user",
                    "content": f"[工作区上下文]\n{workspace_context}\n\n[当前请求]\n{instruction}",
                },
            ],
        }
        result = self._chat_json(payload, request_name="resolve_doc_request", timeout_seconds=settings.llm_timeout_seconds)
        doc = result.get("doc") if isinstance(result.get("doc"), dict) else {}
        logger.info(
            "LLM doc request resolved: sections=%s",
            len(doc.get("sections", [])) if isinstance(doc.get("sections"), list) else 0,
        )
        result["operation"] = "create"
        result["object"] = "doc"
        result["route"] = "doc"
        result.setdefault("reason", "用户要求生成或更新协作文档")
        result.setdefault(
            "plan",
            {
                "goal": instruction[:80],
                "steps": [
                    {
                        "id": "step_1",
                        "type": "sync_doc",
                        "title": "生成并同步协作文档",
                        "depends_on": [],
                    }
                ],
            },
        )
        return result

    def resolve_analysis_request(self, workspace_context: str, instruction: str, route: str) -> dict[str, Any]:
        self._ensure_configured()
        normalized_route = route if route in {"summary", "tasks", "risks"} else "summary"
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._analysis_request_prompt(normalized_route)},
                {
                    "role": "user",
                    "content": f"[工作区上下文]\n{workspace_context}\n\n[当前请求]\n{instruction}",
                },
            ],
        }
        result = self._chat_json(
            payload,
            request_name=f"resolve_{normalized_route}_request",
            timeout_seconds=settings.llm_timeout_seconds,
        )
        result["operation"] = "analyze"
        result["object"] = {
            "tasks": "tasks",
            "risks": "risks",
            "summary": "summary",
        }[normalized_route]
        result["route"] = normalized_route
        result.setdefault("reason", "用户要求分析当前协作上下文")
        result.setdefault(
            "plan",
            {
                "goal": instruction[:80],
                "steps": [
                    {
                        "id": "step_1",
                        "type": "analyze_discussion",
                        "title": "分析讨论并整理结果",
                        "depends_on": [],
                    }
                ],
            },
        )
        logger.info(
            "LLM analysis request resolved: route=%s tasks=%s operations=%s risks=%s",
            normalized_route,
            len(result.get("tasks", [])) if isinstance(result.get("tasks"), list) else 0,
            len(result.get("task_operations", [])) if isinstance(result.get("task_operations"), list) else 0,
            len(result.get("risks", [])) if isinstance(result.get("risks"), list) else 0,
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
        result = self._chat_json(payload, request_name="resolve_workspace_request", timeout_seconds=settings.llm_timeout_seconds)
        logger.info(
            "LLM workspace request resolved: operation=%s object=%s tasks=%s operations=%s plan_steps=%s",
            result.get("operation"),
            result.get("object"),
            len(result.get("tasks", [])) if isinstance(result.get("tasks"), list) else 0,
            len(result.get("task_operations", [])) if isinstance(result.get("task_operations"), list) else 0,
            len(result.get("plan", {}).get("steps", []))
            if isinstance(result.get("plan"), dict) and isinstance(result.get("plan", {}).get("steps"), list)
            else 0,
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
        result = self._chat_json(
            payload,
            request_name="should_recall_memories",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
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
        return self._chat_json(
            payload,
            request_name="generate_presentation_package",
            timeout_seconds=settings.llm_timeout_seconds,
        )

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

    def _chat_json(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        data = self._post_chat_completion(payload, request_name=request_name, timeout_seconds=timeout_seconds)
        text = self._extract_text(data)
        return self._parse_json(text)

    def _post_chat_completion(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
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
  "operation": "read|analyze|create|update|deliver|help|unknown",
  "object": "tasks|summary|risks|doc|slides|workspace",
  "reason": "short reason in Simplified Chinese",
  "clarification": {{
    "needed": false,
    "question": "question to ask the user when clarification is needed",
    "reason": "why clarification is needed",
    "options": ["option 1", "option 2"],
    "blocking": true
  }},
  "plan": {{
    "goal": "execution goal in Simplified Chinese",
    "steps": [
      {{
        "id": "step_1",
        "type": "analyze_discussion|sync_doc|generate_slides|answer_status|reply_help",
        "title": "step title in Simplified Chinese",
        "depends_on": ["step_0"],
        "notes": "optional execution note"
      }}
    ]
  }},
  "summary": "overall summary in Simplified Chinese",
  "task_operations": [
    {{
      "action": "create|update|remove",
      "match_hint": {{
        "title": "existing task title or empty string",
        "owner": "existing owner or TBD"
      }},
      "task": {{
        "title": "task title",
        "owner": "owner or TBD",
        "priority": "high|medium|low",
        "due_date": "YYYY-MM-DD or TBD",
        "status": "draft|done|cancelled",
        "notes": "supporting discussion snippet"
      }},
      "reason": "why this operation is needed"
    }}
  ],
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
  }},
  "doc": {{
    "title": "document title",
    "sections": [
      {{
        "heading": "section heading",
        "paragraphs": ["paragraph 1", "paragraph 2"]
      }}
    ]
  }}
}}

Rules:
- Today is {today} in Asia/Shanghai.
- Prefer understanding the whole discussion instead of keyword matching.
- Choose operation and object before planning. operation=read means answer from existing context without changing task/document state. operation=analyze means organize or update discussion-derived analysis such as tasks, summary, or risks. operation=create means create a new artifact such as a document or slides. operation=update means modify an existing artifact or task snapshot. operation=deliver means share, export, or archive a result.
- object names the thing being handled. For "show/list/query/current/pending task list" requests, use operation=read, object=tasks, and plan.steps=[answer_status]. For "organize/extract/update the task list" requests, use operation=analyze, object=tasks, and plan.steps=[analyze_discussion].
- Do not use operation=analyze for read-only task/status questions.
- The current discussion block is the primary source of truth for this round. Treat older summaries and task snapshots as background state, not as instructions to rewrite everything.
- Use clarification.needed=true when key execution facts are missing or there are multiple materially different paths that require the user's choice first.
- Always think in terms of an execution plan first, then fill the rest of the fields.
- Typical clarification cases include: the target output format is unclear, the user refers to an ambiguous previous decision, or critical owners / deadlines / audience are missing for a deliverable.
- When clarification.needed=true, still choose the most likely operation/object, write a short clarification.question in Simplified Chinese, provide 2 to 4 concise options when possible, and set clarification.blocking=true if execution should pause before continuing.
- Recent discussion lines may include structured fields like "发言人" and "提及". Treat "提及" as a strong assignee hint in multi-person collaboration.
- Distinguish clearly between the speaker, the mentioned teammate, and the final owner of a task.
- When one teammate assigns work to an @mentioned teammate, prefer the mentioned teammate as the task owner unless the discussion clearly says otherwise.
- For operation=analyze objects summary/tasks/risks, prefer using task_operations to describe how the current discussion changes existing tasks.
- If the current round adds one more assignment, create or update only the related tasks. Do not delete or rewrite unrelated existing tasks.
- If an existing task remains valid and the current round does not explicitly change it, preserve it.
- Use create for new tasks, update for changes to existing tasks, and remove for tasks that are explicitly cancelled or no longer needed.
- match_hint should point to the existing task that needs to be updated or removed, usually by title and owner from the current task snapshot.
- You may also return tasks as a refreshed full task list. If both task_operations and tasks are present, task_operations is the primary source of truth.
- For operation=read, do not return task_operations. For status questions, put the natural-language answer into status_answer. You may also return tasks if useful.
- For slides, fill the slides object with 5 to 7 slides and concise bullets.
- For doc, fill doc.title plus doc.sections with a Feishu-document-ready structure.
- plan.steps should contain the high-level execution steps the agent will actually perform. Use only these step types: analyze_discussion, sync_doc, generate_slides, answer_status, reply_help.
- For operation=analyze and object=summary/tasks/risks, usually include analyze_discussion.
- For operation=create/update and object=doc, usually include sync_doc, and add generate_slides when the user also wants a report outline / PPT / presentation material.
- Requests like “总结成文档”, “写成文档”, “整理成文档”, or “沉淀到文档” are document artifact requests: use operation=create or update, object=doc, not operation=analyze/object=summary.
- For operation=create/update and object=slides, include generate_slides.
- For operation=read, include answer_status.
- For help or unknown, include reply_help.
- If the user asks for a report outline and also wants it written into a document, use operation=create, object=doc and fill both doc and slides when helpful.
- If the request is too vague, use operation=help and object=workspace.
- If the request cannot be safely understood, use operation=unknown and object=workspace.
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
- doc: write the discussion or outline into a Feishu document
- help: user asks what the bot can do, or the request is too vague and needs guidance
- unknown: the request is too ambiguous to safely execute

Schema:
{
  "intent": "summary|tasks|risks|status|slides|doc|help|unknown",
  "confidence": 0.0,
  "reason": "short reason in Simplified Chinese"
}

Rules:
- If the user asks for report outline / presentation / PPT / slides, choose slides.
- If the user asks to整理、沉淀、同步 discussion or outline into a Feishu document, choose doc.
- If the user asks who owns tasks / what is pending / deadlines / progress, choose status.
- If the user only says vague things like 'help me handle this' without enough detail, choose help.
- All reasons must be in Simplified Chinese.
""".strip()

    def _route_prompt(self) -> str:
        return """
You are a lightweight route classifier for a Feishu collaboration agent.
Only decide which business route should handle the user's request. Do not plan, do not generate content, and do not extract tasks.
Return valid JSON only. No markdown, no explanation.

Schema:
{
  "route": "status|summary|tasks|risks|doc|slides|help|unknown",
  "confidence": 0.0,
  "needs_clarification": false,
  "requested_outputs": ["doc"],
  "reason": "short reason in Simplified Chinese"
}

Rules:
- If the user asks to view/query current tasks, owners, deadlines, progress, or status, choose status.
- If the user asks to整理/总结/沉淀/同步/写入/写成 a document, Feishu document, requirement document, or doc, choose doc.
- Requests like “总结成文档”, “写成文档”, “整理成文档”, or “沉淀到文档” are doc requests, not summary requests.
- If the user asks for PPT, slides, presentation, 演示稿, or 汇报大纲, choose slides.
- If the user asks for both a document and PPT/slides, choose route=doc and set requested_outputs=["doc","slides"].
- If the user explicitly says only generate PPT and do not write a document, choose route=slides and set requested_outputs=["slides"].
- If the user asks to extract/update/organize action items or TODOs, choose tasks.
- If the user asks only for risks/blockers/卡点, choose risks.
- If the user asks for a normal conversation summary without artifact words, choose summary.
- If the request is too vague to choose the output, choose unknown and set needs_clarification=true.
- All reasons must be in Simplified Chinese.
""".strip()

    def _doc_request_prompt(self) -> str:
        today = current_local_date().isoformat()
        return f"""
You are a Feishu document drafting agent.
Turn the workspace context into a concise, Feishu-document-ready collaborative document.
Return valid JSON only. No markdown, no explanation.

Schema:
{{
  "reason": "short reason in Simplified Chinese",
  "doc": {{
    "title": "document title in Simplified Chinese",
    "sections": [
      {{
        "heading": "section heading",
        "paragraphs": ["paragraph or bullet 1", "paragraph or bullet 2"]
      }}
    ]
  }}
}}

Rules:
- Today is {today} in Asia/Shanghai.
- Use only information supported by the workspace context and the current request.
- If [当前协作文档] is present, treat the request as a document update: compare it with [本次待同步讨论] or [近期群聊讨论] before writing.
- For document updates, return only sections that need refresh, but each returned section must contain the full updated section content, preserving still-valid existing items from [当前协作文档].
- Prefer stable collaboration sections: 讨论摘要, 任务清单, 风险与卡点, 下一步建议, 演示重点, 建议补充素材.
- If current tasks are present, include them in 任务清单 with owner, due date, priority, and status when available.
- If new discussion changes an owner, due date, status, risk, or next action, update the corresponding section instead of only appending a generic summary.
- Convert relative dates like 今天、明天、本周六、这周日、下周三前 into absolute YYYY-MM-DD dates whenever possible.
- Never invent stale years such as 2024 for relative deadlines.
- If the user asks to revise/update an existing document, preserve the existing section structure when possible and only refresh the relevant sections.
- Keep paragraphs concise and directly usable in a Feishu document.
- Do not output task_operations, slides, status_answer, or a multi-step plan.
- All output must be Simplified Chinese.
""".strip()

    def _analysis_request_prompt(self, route: str) -> str:
        today = current_local_date().isoformat()
        focus = {
            "summary": "summarize the current discussion and decisions",
            "tasks": "extract and update action items / task state",
            "risks": "identify risks, blockers, and next actions",
        }.get(route, "summarize the current discussion and decisions")
        object_name = {
            "summary": "summary",
            "tasks": "tasks",
            "risks": "risks",
        }.get(route, "summary")
        return f"""
You are a Feishu collaboration analysis agent.
Your only job is to {focus}. Do not generate documents, slides, or status answers.
Return valid JSON only. No markdown, no explanation.

Schema:
{{
  "reason": "short reason in Simplified Chinese",
  "summary": "overall summary in Simplified Chinese",
  "task_operations": [
    {{
      "action": "create|update|remove",
      "match_hint": {{
        "title": "existing task title or empty string",
        "owner": "existing owner or TBD"
      }},
      "task": {{
        "title": "task title",
        "owner": "owner or TBD",
        "priority": "high|medium|low",
        "due_date": "YYYY-MM-DD or TBD",
        "status": "draft|done|cancelled",
        "notes": "supporting discussion snippet"
      }},
      "reason": "why this operation is needed"
    }}
  ],
  "tasks": [
    {{
      "title": "task title",
      "owner": "owner or TBD",
      "priority": "high|medium|low",
      "due_date": "YYYY-MM-DD or TBD",
      "status": "draft|done|cancelled",
      "notes": "supporting discussion snippet"
    }}
  ],
  "risks": ["risk in Simplified Chinese"],
  "next_actions": ["next action in Simplified Chinese"]
}}

Rules:
- Today is {today} in Asia/Shanghai.
- Focus route is {route}; object is {object_name}.
- Use the current discussion block as the primary source of truth.
- Existing summaries and task snapshots are background state, not instructions to rewrite everything.
- If the user is only asking for {route}, do not add document or slide content.
- For tasks, use task_operations when changing existing task state. Preserve unrelated existing tasks.
- For summary, include tasks/risks only when they are directly supported by context.
- For risks, prioritize concrete blockers and mitigation-oriented next actions.
- Convert relative dates like 今天、明天、这周五、下周三前 into absolute YYYY-MM-DD dates whenever possible.
- Never invent stale years such as 2024 for relative deadlines.
- All output must be Simplified Chinese.
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
- Requests such as ordinary summary, TODO extraction, current risks, syncing current tasks, generating a report outline, or writing a current-discussion document usually do not need long-term recall.
- Requests asking why something changed, what was discussed earlier, previous decisions, historical adjustments, or comparing current status with older context usually need recall.
- All reasons must be in Simplified Chinese.
""".strip()
