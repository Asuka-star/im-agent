import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.llm_client import OpenAICompatibleJSONClient
from app.services.llm_prompts import LLMPromptBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LLMProvider:
    name: str
    api_key: str
    base_url: str
    model: str


def _clean_setting(value: Any) -> str:
    return str(value or "").strip()


def _normalize_xiaomi_model(value: Any) -> str:
    model = _clean_setting(value)
    aliases = {
        "mimo-v2.5-pro": "mimo-v2.5-pro",
        "mimo-v2-5-pro": "mimo-v2.5-pro",
        "mimo v2.5 pro": "mimo-v2.5-pro",
        "mimo-v2.5": "mimo-v2.5",
        "mimo-v2-5": "mimo-v2.5",
        "mimo v2.5": "mimo-v2.5",
    }
    return aliases.get(model.lower(), model)


class LLMService:
    """OpenAI-compatible client for collaboration analysis and response planning."""

    def __init__(self) -> None:
        self.api_key = _clean_setting(settings.llm_api_key) or _clean_setting(settings.anthropic_auth_token)
        self.base_url = (_clean_setting(settings.llm_base_url) or _clean_setting(settings.anthropic_base_url)).rstrip("/")
        self.model = _clean_setting(settings.llm_model) or _clean_setting(settings.anthropic_model)
        self.xiaomi_api_key = _clean_setting(settings.xiaomi_llm_api_key)
        self.xiaomi_base_url = _clean_setting(settings.xiaomi_llm_base_url).rstrip("/")
        self.xiaomi_model = _normalize_xiaomi_model(settings.xiaomi_llm_model)
        self.client = OpenAICompatibleJSONClient(api_key=self.api_key, base_url=self.base_url)
        self._provider_clients: dict[tuple[str, str, str], OpenAICompatibleJSONClient] = {}
        self.prompts = LLMPromptBuilder()
        self._plan_cache: dict[str, dict[str, Any]] = {}

    def is_configured(self) -> bool:
        return bool(self._configured_providers())

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
        result = self._sanitize_route_result(result)
        logger.info(
            "LLM lightweight route resolved: route=%s confidence=%s clarification=%s",
            result.get("route"),
            result.get("confidence"),
            result.get("needs_clarification"),
        )
        return result

    def interpret_workspace_command(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._workspace_command_prompt(),
            user_content=self._context_request_content(
                self._compact_planning_context(workspace_context, max_chars=2400),
                instruction,
                context_label="command context",
            ),
            temperature=0.0,
        )
        result = self._chat_json(
            payload,
            request_name="interpret_workspace_command",
            timeout_seconds=settings.langgraph_llm_timeout_seconds,
        )
        logger.info(
            "LLM workspace command interpreted: operation=%s object=%s confidence=%s clarification=%s",
            result.get("operation"),
            result.get("object"),
            result.get("confidence"),
            result.get("needs_clarification"),
        )
        return result

    def resolve_doc_request(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        edit_intent: dict[str, Any] | None = None
        if self._instruction_needs_doc_edit_intent(instruction, workspace_context):
            try:
                edit_intent = self.resolve_doc_edit_intent(workspace_context, instruction)
            except Exception as exc:  # pragma: no cover - defensive external LLM fallback
                logger.warning("LLM doc edit intent parsing failed, falling back to doc draft prompt: %s", exc)
        payload = self._json_payload(
            system_prompt=self._doc_request_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.2,
        )
        result = self._chat_json(payload, request_name="resolve_doc_request", timeout_seconds=settings.llm_timeout_seconds)
        if isinstance(edit_intent, dict):
            edit_plan = edit_intent.get("artifact_edit_plan")
            if self._is_meaningful_artifact_edit_plan(edit_plan):
                result["artifact_edit_plan"] = edit_plan
            if edit_intent.get("reason") and not result.get("reason"):
                result["reason"] = edit_intent["reason"]
        result = self._sanitize_doc_request_result(result, workspace_context, instruction)
        doc = result.get("doc") if isinstance(result.get("doc"), dict) else {}
        logger.info(
            "LLM doc request resolved: sections=%s edit_intent=%s",
            len(doc.get("sections", [])) if isinstance(doc.get("sections"), list) else 0,
            bool(edit_intent),
        )
        result["operation"] = result.get("operation") or result.get("action") or "create"
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

    def resolve_requirement_brief(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._requirement_brief_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.2,
        )
        result = self._chat_json(payload, request_name="resolve_requirement_brief", timeout_seconds=settings.llm_timeout_seconds)
        brief = result.get("requirement_brief") if isinstance(result.get("requirement_brief"), dict) else {}
        logger.info(
            "LLM requirement brief resolved: title=%s goals=%s risks=%s",
            brief.get("title"),
            len(brief.get("goals", [])) if isinstance(brief.get("goals"), list) else 0,
            len(brief.get("risks", [])) if isinstance(brief.get("risks"), list) else 0,
        )
        result.setdefault("operation", "analyze")
        result.setdefault("object", "doc")
        result.setdefault("route", "doc")
        result.setdefault("reason", "用户要求沉淀当前需求讨论")
        return result

    def resolve_requirement_workspace(self, context: dict[str, Any]) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self.prompts.requirement_workspace_resolution(),
            user_content=json.dumps(context, ensure_ascii=False),
            temperature=0.0,
        )
        result = self._chat_json(
            payload,
            request_name="resolve_requirement_workspace",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
        logger.info(
            "LLM requirement workspace resolved: action=%s requirement_id=%s confidence=%s",
            result.get("action"),
            result.get("requirement_id"),
            result.get("confidence"),
        )
        return result

    def plan_workspace_request(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        cache_key = self._plan_cache_key(workspace_context, instruction)
        cached = self._plan_cache.get(cache_key)
        if cached is not None:
            logger.info("LLM lightweight DAG plan cache hit")
            return json.loads(json.dumps(cached, ensure_ascii=False))

        payload = self._json_payload(
            system_prompt=self._dag_plan_prompt(),
            user_content=self._context_request_content(
                self._compact_planning_context(workspace_context),
                instruction,
                context_label="轻量规划上下文",
            ),
            temperature=0.0,
        )
        result = self._chat_json(
            payload,
            request_name="plan_workspace_request",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
        result = self._sanitize_planning_result(result)
        self._remember_plan_cache(cache_key, result)
        logger.info(
            "LLM lightweight DAG plan resolved: operation=%s object=%s confidence=%s steps=%s",
            result.get("operation"),
            result.get("object"),
            result.get("confidence"),
            len(result.get("plan", {}).get("steps", []))
            if isinstance(result.get("plan"), dict) and isinstance(result.get("plan", {}).get("steps"), list)
            else 0,
        )
        return result

    def resolve_task_intent(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._task_intent_prompt(),
            user_content=self._context_request_content(
                self._compact_planning_context(workspace_context, max_chars=2400),
                instruction,
                context_label="task context",
            ),
            temperature=0.0,
        )
        result = self._chat_json(
            payload,
            request_name="resolve_task_intent",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
        result = self._sanitize_task_intent_result(result)
        logger.info(
            "LLM task intent resolved: intent=%s status=%s confidence=%s hint=%s clarification=%s",
            result.get("intent"),
            result.get("status"),
            result.get("confidence"),
            result.get("task_hint"),
            result.get("clarification", {}).get("needed") if isinstance(result.get("clarification"), dict) else None,
        )
        return result

    def resolve_doc_edit_intent(self, workspace_context: str, instruction: str) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._doc_edit_intent_prompt(),
            user_content=self._context_request_content(workspace_context, instruction),
            temperature=0.0,
        )
        result = self._chat_json(
            payload,
            request_name="resolve_doc_edit_intent",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
        )
        edit_plan = result.get("artifact_edit_plan") if isinstance(result.get("artifact_edit_plan"), dict) else {}
        ops = edit_plan.get("ops") if isinstance(edit_plan.get("ops"), list) else []
        logger.info(
            "LLM doc edit intent resolved: mutation=%s ops=%s fallback=%s",
            edit_plan.get("mutation_required"),
            len(ops),
            edit_plan.get("fallback"),
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
        result = self._chat_json(
            payload,
            request_name="revise_presentation_package",
            timeout_seconds=settings.llm_timeout_seconds,
        )
        if isinstance(result.get("package"), dict):
            package = dict(result["package"])
            if isinstance(result.get("artifact_edit_plan"), dict):
                package["artifact_edit_plan"] = result["artifact_edit_plan"]
            return package
        return result

    def rerank_next_actions(self, context: dict[str, Any]) -> dict[str, Any]:
        self._ensure_configured()
        payload = self._json_payload(
            system_prompt=self._next_action_rerank_prompt(),
            user_content=json.dumps(context, ensure_ascii=False),
            temperature=0.1,
        )
        return self._chat_json(
            payload,
            request_name="rerank_next_actions",
            timeout_seconds=settings.llm_memory_gate_timeout_seconds,
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
        providers = self._configured_providers()
        if not providers:
            raise RuntimeError("LLM config is incomplete.")
        last_error: Exception | None = None
        for index, provider in enumerate(providers):
            provider_payload = {**payload, "model": provider.model}
            try:
                result = self._client_for_provider(provider).chat_json(
                    provider_payload,
                    request_name=request_name,
                    timeout_seconds=timeout_seconds,
                )
                if index > 0:
                    logger.info(
                        "LLM fallback provider succeeded: request=%s provider=%s model=%s",
                        request_name,
                        provider.name,
                        provider.model,
                )
                return result
            except Exception as exc:
                if provider.name == "xiaomi" and "response_format" in provider_payload:
                    try:
                        relaxed_payload = dict(provider_payload)
                        relaxed_payload.pop("response_format", None)
                        result = self._client_for_provider(provider).chat_json(
                            relaxed_payload,
                            request_name=request_name,
                            timeout_seconds=timeout_seconds,
                        )
                        logger.info(
                            "LLM provider succeeded without response_format: request=%s provider=%s model=%s",
                            request_name,
                            provider.name,
                            provider.model,
                        )
                        return result
                    except Exception as relaxed_exc:
                        logger.warning(
                            "LLM provider retry without response_format failed: request=%s provider=%s error=%s",
                            request_name,
                            provider.name,
                            relaxed_exc,
                        )
                last_error = exc
                if index + 1 >= len(providers):
                    break
                fallback = providers[index + 1]
                logger.warning(
                    "LLM provider failed, retrying fallback: request=%s provider=%s fallback=%s error=%s",
                    request_name,
                    provider.name,
                    fallback.name,
                    exc,
                )
        assert last_error is not None
        raise last_error

    def _post_chat_completion(self, payload: dict[str, Any], *, request_name: str, timeout_seconds: float) -> dict[str, Any]:
        return self.client.post_chat_completion(payload, request_name=request_name, timeout_seconds=timeout_seconds)

    def _extract_text(self, payload: dict[str, Any]) -> str:
        return self.client.extract_text(payload)

    def _parse_json(self, text: str) -> dict[str, Any]:
        return self.client.parse_json(text)

    def _configured_providers(self) -> list[_LLMProvider]:
        providers: list[_LLMProvider] = []
        xiaomi_api_key = _clean_setting(self.xiaomi_api_key)
        xiaomi_base_url = _clean_setting(self.xiaomi_base_url).rstrip("/")
        xiaomi_model = _normalize_xiaomi_model(self.xiaomi_model)
        api_key = _clean_setting(self.api_key)
        base_url = _clean_setting(self.base_url).rstrip("/")
        model = _clean_setting(self.model)
        if xiaomi_api_key and xiaomi_base_url and xiaomi_model:
            providers.append(
                _LLMProvider(
                    name="xiaomi",
                    api_key=xiaomi_api_key,
                    base_url=xiaomi_base_url,
                    model=xiaomi_model,
                )
            )
        if api_key and base_url and model:
            providers.append(
                _LLMProvider(
                    name="deepseek",
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                )
            )
        return providers

    def _client_for_provider(self, provider: _LLMProvider) -> OpenAICompatibleJSONClient:
        key = (provider.name, provider.api_key, provider.base_url)
        client = self._provider_clients.get(key)
        if client is None:
            client = OpenAICompatibleJSONClient(api_key=provider.api_key, base_url=provider.base_url)
            self._provider_clients[key] = client
        return client

    def _extraction_prompt(self) -> str:
        return self.prompts.extraction()

    def _workspace_request_prompt(self) -> str:
        return self.prompts.workspace_request()

    def _dag_plan_prompt(self) -> str:
        return self.prompts.dag_plan()

    def _presentation_prompt(self) -> str:
        return self.prompts.presentation()

    def _presentation_revision_prompt(self) -> str:
        return self.prompts.presentation_revision()

    def _next_action_rerank_prompt(self) -> str:
        return self.prompts.next_action_rerank()

    def _intent_prompt(self) -> str:
        return self.prompts.intent()

    def _route_prompt(self) -> str:
        return self.prompts.route()

    def _workspace_command_prompt(self) -> str:
        return self.prompts.workspace_command()

    def _task_intent_prompt(self) -> str:
        return self.prompts.task_intent()

    def _doc_request_prompt(self) -> str:
        return self.prompts.doc_request()

    def _requirement_brief_prompt(self) -> str:
        return self.prompts.requirement_brief()

    def _doc_edit_intent_prompt(self) -> str:
        return self.prompts.doc_edit_intent()

    def _analysis_request_prompt(self, route: str) -> str:
        return self.prompts.analysis_request(route)

    def _memory_gate_prompt(self) -> str:
        return self.prompts.memory_gate()

    @classmethod
    def _sanitize_doc_request_result(cls, result: dict[str, Any], workspace_context: str, instruction: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        doc = result.get("doc")
        if not isinstance(doc, dict):
            return result
        sections = doc.get("sections")
        if not isinstance(sections, list):
            return result

        grounding_text = cls._implementation_grounding_text(workspace_context, instruction)
        sanitized_sections: list[Any] = []
        for section in sections:
            if not isinstance(section, dict):
                sanitized_sections.append(section)
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs")
            if not isinstance(paragraphs, list):
                sanitized_sections.append(section)
                continue

            if cls._is_implementation_section_heading(heading):
                kept: list[Any] = []
                removed_specifics = False
                for paragraph in paragraphs:
                    text = str(paragraph or "").strip()
                    if not text:
                        continue
                    if cls._has_ungrounded_plan_specifics(text, grounding_text):
                        removed_specifics = True
                        continue
                    kept.append(paragraph)
                if removed_specifics:
                    fallback = "具体实施计划、负责人和截止时间尚未在本轮讨论中明确，需后续确认。"
                    if fallback not in [str(item).strip() for item in kept]:
                        kept.insert(0, fallback)
                section = {**section, "paragraphs": kept or ["具体实施计划、负责人和截止时间尚未在本轮讨论中明确，需后续确认。"]}
                sanitized_sections.append(section)
                continue

            if cls._is_technical_section_heading(heading):
                kept = []
                removed_specifics = False
                for paragraph in paragraphs:
                    text = str(paragraph or "").strip()
                    if not text:
                        continue
                    if cls._has_ungrounded_technical_specifics(text, grounding_text):
                        removed_specifics = True
                        continue
                    kept.append(paragraph)
                if removed_specifics:
                    fallback = "技术栈、数据库、部署方式和系统集成方案尚未在当前需求讨论或文档中明确，需后续确认。"
                    if fallback not in [str(item).strip() for item in kept]:
                        kept.insert(0, fallback)
                section = {**section, "paragraphs": kept or ["技术栈、数据库、部署方式和系统集成方案尚未在当前需求讨论或文档中明确，需后续确认。"]}
                sanitized_sections.append(section)
                continue

            sanitized_sections.append(section)

        result = {**result, "doc": {**doc, "sections": sanitized_sections}}
        return result

    @staticmethod
    def _is_implementation_section_heading(heading: str) -> bool:
        return any(marker in heading for marker in ("实施计划", "分工", "里程碑", "交付计划"))

    @classmethod
    def _is_technical_section_heading(cls, heading: str) -> bool:
        return any(marker in str(heading or "") for marker in ("技术方案", "技术架构", "系统架构", "实现方案"))

    @classmethod
    def _has_ungrounded_technical_specifics(cls, text: str, grounding_text: str) -> bool:
        normalized_text = str(text or "").strip()
        normalized_grounding = str(grounding_text or "")
        if not normalized_text:
            return False
        if any(marker in normalized_text for marker in ("待确认", "未明确", "尚未明确", "需后续确认", "待后续确认")):
            return False
        text_lower = normalized_text.lower()
        grounding_lower = normalized_grounding.lower()
        concrete_terms = (
            "前后端分离",
            "vue",
            "react",
            "angular",
            "spring boot",
            "springboot",
            "node.js",
            "nodejs",
            "express",
            "nest.js",
            "nestjs",
            "django",
            "flask",
            "fastapi",
            "mysql",
            "postgresql",
            "postgres",
            "redis",
            "mongodb",
            "elasticsearch",
            "docker",
            "kubernetes",
            "k8s",
            "java",
            "python",
            "typescript",
            "javascript",
            "数据库采用",
            "部署",
            "系统集成",
            "教务系统",
        )
        for term in concrete_terms:
            term_lower = term.lower()
            if term_lower in text_lower and term_lower not in grounding_lower:
                return True
        return False

    @classmethod
    def _has_ungrounded_plan_specifics(cls, text: str, grounding_text: str) -> bool:
        normalized_text = str(text or "").strip()
        normalized_grounding = str(grounding_text or "")
        if not normalized_text:
            return False
        if cls._contains_ungrounded_plan_date(normalized_text, normalized_grounding):
            return True
        if cls._contains_ungrounded_plan_owner(normalized_text, normalized_grounding):
            return True
        return False

    @staticmethod
    def _contains_ungrounded_plan_date(text: str, grounding_text: str) -> bool:
        date_patterns = (
            r"20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?",
            r"\d{1,2}月\s*(?:[-至到~]\s*\d{1,2}月)?",
        )
        for pattern in date_patterns:
            for match in re.finditer(pattern, text):
                value = match.group(0)
                if value and value not in grounding_text:
                    return True
        return False

    @classmethod
    def _contains_ungrounded_plan_owner(cls, text: str, grounding_text: str) -> bool:
        if "负责人" not in text and "owner" not in text.lower():
            return False
        for match in re.finditer(r"(?:负责人|owner)\s*[:：]\s*([^，,；;。\n]+)", text, flags=re.IGNORECASE):
            owner = match.group(1).strip(" -｜|：:。；;，,")
            if not owner or owner.upper() == "TBD" or owner in {"待确认", "待定", "未定"}:
                continue
            if not cls._has_grounded_owner_assignment(owner, grounding_text):
                return True
        return False

    @staticmethod
    def _has_grounded_owner_assignment(owner: str, grounding_text: str) -> bool:
        if not owner:
            return False
        for line in str(grounding_text or "").splitlines():
            if owner not in line:
                continue
            if "发言人" in line and "负责人" not in line and "负责" not in line:
                continue
            if any(marker in line for marker in ("负责人", "负责", "分工", "由", "来做", "认领", "owner")):
                return True
        return False

    @staticmethod
    def _implementation_grounding_text(workspace_context: str, instruction: str) -> str:
        context = str(workspace_context or "")
        blocks: list[str] = [str(instruction or "")]
        allowed_headings = (
            "[当前需求工作区]",
            "[当前需求讨论事实]",
            "[当前需求已有产物]",
            "[当前需求运行记录]",
            "[需求边界约束]",
            "[当前协作文档]",
            "[本次待同步讨论]",
            "[近期群聊讨论]",
            "[实施计划参考]",
            "[实施计划变更参考]",
            "[任务变更记录]",
            "[当前任务快照]",
        )
        lines = context.splitlines()
        collecting = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                collecting = stripped in allowed_headings
                if collecting:
                    blocks.append(stripped)
                continue
            if collecting:
                blocks.append(line)
        return "\n".join(blocks)

    @staticmethod
    def _compact_planning_context(workspace_context: str, *, max_chars: int = 3000) -> str:
        text = str(workspace_context or "").strip()
        if len(text) <= max_chars:
            return text
        head = text[: max_chars // 2].rstrip()
        tail = text[-max_chars // 2 :].lstrip()
        return f"{head}\n...\n{tail}"

    @staticmethod
    def _plan_cache_key(workspace_context: str, instruction: str) -> str:
        payload = json.dumps(
            {
                "context": LLMService._compact_planning_context(workspace_context),
                "instruction": instruction,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _remember_plan_cache(self, cache_key: str, result: dict[str, Any]) -> None:
        if len(self._plan_cache) >= 128:
            oldest_key = next(iter(self._plan_cache))
            self._plan_cache.pop(oldest_key, None)
        self._plan_cache[cache_key] = json.loads(json.dumps(result, ensure_ascii=False))

    @classmethod
    def _sanitize_route_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        if "route" not in result and "intent" in result:
            result = {**result, "route": result.get("intent")}
        allowed_keys = {
            "route",
            "intent",
            "confidence",
            "needs_clarification",
            "requested_outputs",
            "reason",
        }
        sanitized = {key: result[key] for key in allowed_keys if key in result}
        if "confidence" in sanitized:
            sanitized["confidence"] = cls._normalize_confidence(sanitized["confidence"])
        if "needs_clarification" in sanitized:
            sanitized["needs_clarification"] = cls._normalize_bool(sanitized["needs_clarification"])
        if "requested_outputs" in sanitized:
            sanitized["requested_outputs"] = cls._sanitize_requested_outputs(sanitized["requested_outputs"])
        return sanitized

    @classmethod
    def _sanitize_planning_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        if "operation" not in result and "action" in result:
            result = {**result, "operation": result.get("action")}
        if "object" not in result and "target" in result:
            result = {**result, "object": result.get("target")}
        allowed_keys = {
            "operation",
            "object",
            "route",
            "intent",
            "confidence",
            "reason",
            "requested_outputs",
            "plan",
            "clarification",
        }
        sanitized = {key: result[key] for key in allowed_keys if key in result}
        if "requested_outputs" in sanitized:
            sanitized["requested_outputs"] = cls._sanitize_requested_outputs(sanitized["requested_outputs"])
        if "plan" in sanitized:
            sanitized["plan"] = cls._sanitize_plan(sanitized["plan"])
        if "clarification" in sanitized:
            sanitized["clarification"] = cls._sanitize_clarification(sanitized["clarification"])
        return sanitized

    @classmethod
    def _sanitize_task_intent_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"intent": "unknown", "confidence": 0.0}
        intent = cls._normalize_task_intent(result.get("intent") or result.get("action"))
        status = cls._normalize_task_status(result.get("status"))
        actor = cls._sanitize_task_party(result.get("actor"), allowed_sources={"sender", "literal", "mentioned", "unknown"})
        assignee = cls._sanitize_task_party(
            result.get("assignee"),
            allowed_sources={"sender", "literal", "mentioned", "tbd", "unknown"},
        )
        sanitized = {
            "intent": intent,
            "actor": actor,
            "task_hint": str(result.get("task_hint") or result.get("title") or "").strip()[:120],
            "status": status,
            "assignee": assignee,
            "confidence": cls._normalize_confidence(result.get("confidence")),
            "requires_existing_task": True if result.get("requires_existing_task") is None else bool(result.get("requires_existing_task")),
            "reason": str(result.get("reason") or "").strip()[:240],
        }
        if "clarification" in result:
            sanitized["clarification"] = cls._sanitize_clarification(result.get("clarification"))
        return sanitized

    @staticmethod
    def _normalize_task_intent(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "status_update": "task_status_update",
            "complete_task": "task_status_update",
            "completion": "task_status_update",
            "cancel_task": "task_status_update",
            "assign_task": "task_assignment",
            "create_task": "task_assignment",
            "claim_task": "task_assignment",
            "tasks": "task_query",
            "status": "task_query",
            "query": "task_query",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized in {"task_status_update", "task_assignment", "task_query", "unknown"}:
            return normalized
        return "unknown"

    @staticmethod
    def _normalize_task_status(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "completed": "done",
            "complete": "done",
            "finished": "done",
            "finish": "done",
            "cancel": "cancelled",
            "canceled": "cancelled",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized in {"done", "draft", "cancelled", "unknown"}:
            return normalized
        return "unknown"

    @staticmethod
    def _sanitize_task_party(value: Any, *, allowed_sources: set[str]) -> dict[str, str]:
        payload = value if isinstance(value, dict) else {}
        source = str(payload.get("source") or "unknown").strip().lower().replace("-", "_")
        if source not in allowed_sources:
            source = "unknown"
        return {
            "text": str(payload.get("text") or "").strip()[:80],
            "source": source,
        }

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(confidence, 1.0))

    @staticmethod
    def _normalize_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        normalized = str(value or "").strip().lower()
        return normalized in {"true", "yes", "y", "1", "需要", "是"}

    @staticmethod
    def _sanitize_requested_outputs(value: Any) -> list[str]:
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list):
            raw_items = value
        else:
            return []
        outputs: list[str] = []
        aliases = {
            "document": "doc",
            "feishu_doc": "doc",
            "ppt": "slides",
            "presentation": "slides",
            "deck": "slides",
            "whiteboard": "canvas",
            "board": "canvas",
            "diagram": "canvas",
            "flowchart": "canvas",
        }
        for item in raw_items:
            normalized = str(item or "").strip().lower().replace("-", "_")
            normalized = aliases.get(normalized, normalized)
            if normalized in {"doc", "slides", "canvas"} and normalized not in outputs:
                outputs.append(normalized)
        return outputs

    @classmethod
    def _sanitize_plan(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        steps: list[dict[str, Any]] = []
        raw_steps = value.get("steps") if isinstance(value.get("steps"), list) else []
        allowed_step_types = {
            "analyze_discussion",
            "sync_doc",
            "generate_slides",
            "generate_canvas",
            "answer_status",
            "reply_help",
        }
        for index, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                continue
            step_type = cls._normalize_plan_step_type(item.get("type") or item.get("step_type"))
            if step_type not in allowed_step_types:
                continue
            step = {
                "id": str(item.get("id") or item.get("step_id") or f"step_{index}").strip() or f"step_{index}",
                "type": step_type,
                "title": str(item.get("title") or "").strip()[:120],
                "depends_on": [
                    str(dep).strip()
                    for dep in item.get("depends_on", [])
                    if dep is not None and str(dep).strip()
                ]
                if isinstance(item.get("depends_on"), list)
                else [],
            }
            notes = str(item.get("notes") or "").strip()
            if notes:
                step["notes"] = notes[:240]
            steps.append(step)
        valid_step_ids = {step["id"] for step in steps}
        for step in steps:
            step["depends_on"] = [dep for dep in step["depends_on"] if dep in valid_step_ids and dep != step["id"]]
        return {
            "goal": str(value.get("goal") or "").strip()[:160],
            "steps": steps,
        }

    @staticmethod
    def _normalize_plan_step_type(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        mapping = {
            "analyze_discussion": "analyze_discussion",
            "analyze": "analyze_discussion",
            "summary": "analyze_discussion",
            "summarize_context": "analyze_discussion",
            "sync_doc": "sync_doc",
            "generate_doc": "sync_doc",
            "write_doc": "sync_doc",
            "doc": "sync_doc",
            "document": "sync_doc",
            "feishu_doc": "sync_doc",
            "generate_slides": "generate_slides",
            "slides": "generate_slides",
            "slide": "generate_slides",
            "ppt": "generate_slides",
            "presentation": "generate_slides",
            "deck": "generate_slides",
            "generate_canvas": "generate_canvas",
            "canvas": "generate_canvas",
            "whiteboard": "generate_canvas",
            "board": "generate_canvas",
            "diagram": "generate_canvas",
            "flowchart": "generate_canvas",
            "answer_status": "answer_status",
            "status": "answer_status",
            "reply_help": "reply_help",
            "help": "reply_help",
        }
        return mapping.get(normalized, "")

    @staticmethod
    def _sanitize_clarification(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        options = (
            [str(item).strip() for item in value.get("options", []) if str(item).strip()][:4]
            if isinstance(value.get("options"), list)
            else []
        )
        return {
            "needed": bool(value.get("needed")),
            "question": str(value.get("question") or "").strip()[:200],
            "reason": str(value.get("reason") or "").strip()[:240],
            "options": options,
            "blocking": True if value.get("blocking") is None else bool(value.get("blocking")),
        }

    @staticmethod
    def _instruction_needs_doc_edit_intent(instruction: str, workspace_context: str = "") -> bool:
        instruction_text = instruction.lower()
        context_text = workspace_context[:1200].lower()
        mutation_markers = (
            "删除",
            "删掉",
            "移除",
            "去掉",
            "清空",
            "更新",
            "修改",
            "改写",
            "重写",
            "重新整理",
            "补充",
            "新增",
            "添加",
            "追加",
            "重命名",
            "改名",
            "改成",
            "移动",
            "调整",
            "排序",
            "规范",
            "格式",
            "压缩",
            "精简",
            "delete",
            "remove",
            "clear",
            "update",
            "modify",
            "rewrite",
            "append",
            "add",
            "rename",
            "move",
            "format",
            "compress",
        )
        target_markers = ("文档", "doc", "document", "小节", "栏目", "标题", "段落", "section", "heading")
        has_mutation = any(marker in instruction_text for marker in mutation_markers)
        has_target = any(marker in instruction_text for marker in target_markers) or "[当前协作文档]" in workspace_context
        if has_mutation and has_target:
            return True
        range_markers = ("后面", "之后", "以下", "以前", "之前", "以上", "这一组", "本组", "之间", "below", "after", "before", "between")
        return has_mutation and any(marker in instruction_text for marker in range_markers) and bool(context_text)

    @staticmethod
    def _is_meaningful_artifact_edit_plan(edit_plan: Any) -> bool:
        if not isinstance(edit_plan, dict):
            return False
        ops = edit_plan.get("ops")
        if isinstance(ops, list) and ops:
            return True
        return bool(edit_plan.get("mutation_required"))
