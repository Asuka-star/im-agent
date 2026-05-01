import json
import logging
from typing import Any

from app.core.config import settings
from app.services.llm_client import OpenAICompatibleJSONClient
from app.services.llm_prompts import LLMPromptBuilder

logger = logging.getLogger(__name__)


class LLMService:
    """OpenAI-compatible client for collaboration analysis and response planning."""

    def __init__(self) -> None:
        self.api_key = settings.llm_api_key or settings.anthropic_auth_token
        self.base_url = (settings.llm_base_url or settings.anthropic_base_url).rstrip("/")
        self.model = settings.llm_model or settings.anthropic_model
        self.client = OpenAICompatibleJSONClient(api_key=self.api_key, base_url=self.base_url)
        self.prompts = LLMPromptBuilder()

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def extract_collaboration(self, raw_text: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._extraction_prompt(),
            user_content=raw_text,
            temperature=0.2,
        )
        result = self._chat_json(payload, request_name="extract_collaboration", timeout_seconds=settings.llm_timeout_seconds)
        logger.info("LLM extraction succeeded with %s task(s)", len(result.get("tasks", [])))
        return result

    def classify_intent(self, user_text: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._intent_prompt(),
            user_content=user_text,
            temperature=0.0,
        )
        result = self._chat_json(payload, request_name="classify_intent", timeout_seconds=settings.llm_timeout_seconds)
        logger.info(
            "LLM intent classification succeeded: intent=%s confidence=%s",
            result.get("intent"),
            result.get("confidence"),
        )
        return result

    def route_workspace_request(self, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._route_prompt(),
            user_content=instruction,
            temperature=0.0,
        )
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
        payload = self._json_payload(
            system_prompt=self._doc_request_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.2,
        )
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
        payload = self._json_payload(
            system_prompt=self._analysis_request_prompt(normalized_route),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.2,
        )
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
        payload = self._json_payload(
            system_prompt=self._workspace_request_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.2,
        )
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
        payload = self._json_payload(
            system_prompt=self._memory_gate_prompt(),
            user_content=self._context_request_content(workspace_context, instruction, context_label="轻量上下文"),
            temperature=0.0,
        )
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
        payload = self._json_payload(
            system_prompt=self._presentation_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.4,
        )
        return self._chat_json(
            payload,
            request_name="generate_presentation_package",
            timeout_seconds=settings.llm_timeout_seconds,
        )

    def revise_presentation_package(self, package: dict[str, Any], workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._presentation_revision_prompt(),
            user_content=json.dumps(
                {
                    "current_package": package,
                    "workspace_context": workspace_context,
                    "revision_instruction": instruction,
                },
                ensure_ascii=False,
            ),
            temperature=0.25,
        )
        return self._chat_json(
            payload,
            request_name="revise_presentation_package",
            timeout_seconds=settings.llm_timeout_seconds,
        )

    def _ensure_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError("LLM config is incomplete.")

    def _json_payload(self, *, system_prompt: str, user_content: str, temperature: float) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }

    @staticmethod
    def _context_request_content(workspace_context: str, instruction: str, *, context_label: str = "工作区上下文") -> str:
        return f"[{context_label}]\n{workspace_context}\n\n[当前请求]\n{instruction}"

    def _chat_json(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        return self.client.chat_json(payload, request_name=request_name, timeout_seconds=timeout_seconds)

    def _post_chat_completion(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        return self.client.post_chat_completion(payload, request_name=request_name, timeout_seconds=timeout_seconds)

    def _extract_text(self, payload: dict[str, Any]) -> str:
        return self.client.extract_text(payload)

    def _parse_json(self, text: str) -> dict[str, Any]:
        return self.client.parse_json(text)

    def _extraction_prompt(self) -> str:
        return self.prompts.extraction()

    def _workspace_request_prompt(self) -> str:
        return self.prompts.workspace_request()

    def _presentation_prompt(self) -> str:
        return self.prompts.presentation()

    def _presentation_revision_prompt(self) -> str:
        return self.prompts.presentation_revision()

    def _intent_prompt(self) -> str:
        return self.prompts.intent()

    def _route_prompt(self) -> str:
        return self.prompts.route()

    def _doc_request_prompt(self) -> str:
        return self.prompts.doc_request()

    def _analysis_request_prompt(self, route: str) -> str:
        return self.prompts.analysis_request(route)

    def _memory_gate_prompt(self) -> str:
        return self.prompts.memory_gate()
