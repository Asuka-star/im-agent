import hashlib
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
        self._plan_cache: dict[str, dict[str, Any]] = {}

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
        result = self._sanitize_route_result(result)
        logger.info(
            "LLM lightweight route resolved: route=%s confidence=%s clarification=%s",
            result.get("route"),
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

    def _doc_request_prompt(self) -> str:
        return self.prompts.doc_request()

    def _doc_edit_intent_prompt(self) -> str:
        return self.prompts.doc_edit_intent()

    def _analysis_request_prompt(self, route: str) -> str:
        return self.prompts.analysis_request(route)

    def _memory_gate_prompt(self) -> str:
        return self.prompts.memory_gate()

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
