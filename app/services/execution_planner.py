from __future__ import annotations

from dataclasses import dataclass

from app.schemas.planner import ExecutionPlan, PlannerStep


@dataclass(frozen=True, slots=True)
class RequestProtocol:
    operation: str
    object: str
    route: str


class ExecutionPlanner:
    """Normalizes agent routes and builds executable plans."""

    def normalize_request_protocol(self, llm_result: dict) -> RequestProtocol:
        raw_route = self.normalize_token(llm_result.get("route") or llm_result.get("intent"))
        raw_operation = self.normalize_operation(llm_result.get("operation") or llm_result.get("action"))
        raw_object = self.normalize_object(llm_result.get("object") or llm_result.get("target"))

        if not raw_operation or not raw_object:
            inferred = self.infer_protocol_from_legacy_result(llm_result, raw_route)
            raw_operation = raw_operation or inferred.operation
            raw_object = raw_object or inferred.object

        route = self.canonical_route(raw_operation, raw_object, raw_route)
        protocol = RequestProtocol(
            operation=raw_operation or "help",
            object=raw_object or "workspace",
            route=route,
        )
        self.store_request_protocol(llm_result, protocol)
        return protocol

    @staticmethod
    def store_request_protocol(llm_result: dict, protocol: RequestProtocol) -> None:
        llm_result["operation"] = protocol.operation
        llm_result["object"] = protocol.object
        llm_result["route"] = protocol.route
        llm_result["request_protocol"] = {
            "operation": protocol.operation,
            "object": protocol.object,
            "route": protocol.route,
        }

    def adjust_protocol_for_instruction(
        self,
        protocol: RequestProtocol,
        *,
        instruction: str,
        llm_result: dict,
    ) -> RequestProtocol:
        if protocol.object in {"doc", "slides"}:
            return protocol
        if not (
            self.instruction_requests_doc(instruction)
            or self.llm_result_has_doc_package(llm_result)
            or self.llm_plan_contains_step(llm_result, "sync_doc")
        ):
            return protocol

        operation = "update" if protocol.operation == "update" else "create"
        adjusted = RequestProtocol(operation=operation, object="doc", route="doc")
        self.store_request_protocol(llm_result, adjusted)
        return adjusted

    @staticmethod
    def instruction_requests_doc(instruction: str) -> bool:
        text = str(instruction or "").strip().lower()
        if not text:
            return False
        explicit_doc_keywords = (
            "文档",
            "doc",
            "document",
            "飞书文档",
            "协作文档",
            "需求文档",
            "写成材料",
            "整理成材料",
            "沉淀成材料",
        )
        return any(keyword in text for keyword in explicit_doc_keywords)

    @staticmethod
    def llm_result_has_doc_package(llm_result: dict) -> bool:
        doc = llm_result.get("doc") if isinstance(llm_result, dict) else None
        return isinstance(doc, dict) and isinstance(doc.get("sections"), list) and bool(doc.get("sections"))

    def llm_plan_contains_step(self, llm_result: dict, step_type: str) -> bool:
        raw_plan = llm_result.get("plan") if isinstance(llm_result, dict) else None
        raw_steps = raw_plan.get("steps") if isinstance(raw_plan, dict) else None
        if not isinstance(raw_steps, list):
            return False
        for item in raw_steps:
            if not isinstance(item, dict):
                continue
            normalized = self.normalize_plan_step_type(str(item.get("type") or item.get("step_type") or ""))
            if normalized == step_type:
                return True
        return False

    @staticmethod
    def normalize_token(value: object) -> str:
        return str(value or "").strip().lower().replace("-", "_")

    def normalize_operation(self, value: object) -> str:
        token = self.normalize_token(value)
        aliases = {
            "read": "read",
            "query": "read",
            "get": "read",
            "list": "read",
            "show": "read",
            "status": "read",
            "answer": "read",
            "analyze": "analyze",
            "analysis": "analyze",
            "summarize": "analyze",
            "summary": "analyze",
            "organize": "analyze",
            "extract": "analyze",
            "create": "create",
            "generate": "create",
            "write": "create",
            "sync": "create",
            "update": "update",
            "revise": "update",
            "modify": "update",
            "deliver": "deliver",
            "share": "deliver",
            "archive": "deliver",
            "export": "deliver",
            "help": "help",
            "clarify": "help",
            "unknown": "unknown",
        }
        return aliases.get(token, "")

    def normalize_object(self, value: object) -> str:
        token = self.normalize_token(value)
        aliases = {
            "task": "tasks",
            "tasks": "tasks",
            "todo": "tasks",
            "todos": "tasks",
            "action_items": "tasks",
            "summary": "summary",
            "summaries": "summary",
            "risk": "risks",
            "risks": "risks",
            "blocker": "risks",
            "blockers": "risks",
            "doc": "doc",
            "document": "doc",
            "feishu_doc": "doc",
            "docs": "doc",
            "slides": "slides",
            "slide": "slides",
            "ppt": "slides",
            "presentation": "slides",
            "deck": "slides",
            "canvas": "canvas",
            "whiteboard": "canvas",
            "board": "canvas",
            "diagram": "canvas",
            "flowchart": "canvas",
            "workspace": "workspace",
            "help": "workspace",
            "unknown": "workspace",
        }
        return aliases.get(token, "")

    def infer_protocol_from_legacy_result(self, llm_result: dict, route: str) -> RequestProtocol:
        if route == "status" or str(llm_result.get("status_answer") or "").strip():
            return RequestProtocol(operation="read", object="tasks", route="status")
        if route == "tasks":
            return RequestProtocol(operation="analyze", object="tasks", route="tasks")
        if route == "summary":
            return RequestProtocol(operation="analyze", object="summary", route="summary")
        if route == "risks":
            return RequestProtocol(operation="analyze", object="risks", route="risks")
        if route == "doc":
            return RequestProtocol(operation="create", object="doc", route="doc")
        if route == "slides":
            return RequestProtocol(operation="create", object="slides", route="slides")
        if route == "canvas":
            return RequestProtocol(operation="create", object="canvas", route="canvas")
        if route == "unknown":
            return RequestProtocol(operation="unknown", object="workspace", route="unknown")
        return RequestProtocol(operation="help", object="workspace", route="help")

    @staticmethod
    def canonical_route(operation: str, object_name: str, fallback: str) -> str:
        if operation == "read":
            return "status"
        if operation == "analyze":
            if object_name in {"summary", "risks"}:
                return object_name
            if object_name == "tasks":
                return "tasks"
            return fallback if fallback in {"summary", "tasks", "risks"} else "summary"
        if operation == "update":
            if object_name in {"summary", "risks"}:
                return object_name
            if object_name == "tasks":
                return "tasks"
            if object_name in {"doc", "slides", "canvas"}:
                return object_name
            return fallback if fallback in {"summary", "tasks", "risks", "doc", "slides", "canvas"} else "help"
        if operation == "create":
            if object_name in {"doc", "slides", "canvas"}:
                return object_name
            if object_name == "tasks":
                return "tasks"
            return fallback if fallback in {"doc", "slides"} else "doc"
        if operation == "deliver":
            return "help"
        if operation == "unknown":
            return "unknown"
        return "help"

    @staticmethod
    def allowed_steps_for_protocol(protocol: RequestProtocol) -> set[str]:
        if protocol.operation == "read":
            return {"answer_status"}
        if protocol.operation == "analyze":
            return {"analyze_discussion"}
        if protocol.operation in {"create", "update"}:
            if protocol.object == "doc":
                return {"sync_doc", "generate_slides"}
            if protocol.object == "slides":
                return {"generate_slides"}
            if protocol.object == "canvas":
                return {"generate_canvas"}
            if protocol.object == "tasks":
                return {"analyze_discussion"}
            return {"reply_help"}
        return {"reply_help"}

    @staticmethod
    def required_step_for_protocol(protocol: RequestProtocol) -> str:
        if protocol.operation == "read":
            return "answer_status"
        if protocol.operation == "analyze":
            return "analyze_discussion"
        if protocol.operation in {"create", "update"} and protocol.object == "doc":
            return "sync_doc"
        if protocol.operation in {"create", "update"} and protocol.object == "slides":
            return "generate_slides"
        if protocol.operation in {"create", "update"} and protocol.object == "canvas":
            return "generate_canvas"
        if protocol.operation == "update" and protocol.object == "tasks":
            return "analyze_discussion"
        return "reply_help"

    def resolve_execution_plan(
        self,
        *,
        intent: str,
        reason: str,
        llm_result: dict,
        instruction: str,
        protocol: RequestProtocol | None = None,
    ) -> ExecutionPlan:
        if protocol is None:
            if (
                llm_result.get("operation")
                or llm_result.get("object")
                or llm_result.get("action")
                or llm_result.get("target")
                or llm_result.get("route")
            ):
                protocol = self.normalize_request_protocol(llm_result)
            else:
                protocol = self.infer_protocol_from_legacy_result(llm_result, self.normalize_token(intent))
        protocol = self.adjust_protocol_for_instruction(
            protocol,
            instruction=instruction,
            llm_result=llm_result,
        )
        raw_plan = llm_result.get("plan")
        if isinstance(raw_plan, dict):
            normalized = self.normalize_execution_plan(
                raw_plan,
                intent=intent,
                instruction=instruction,
            )
            if normalized.steps:
                return self.enforce_protocol_on_plan(
                    normalized,
                    protocol=protocol,
                    reason=reason,
                    instruction=instruction,
                    llm_result=llm_result,
                )
        plan = self.build_fallback_plan(
            intent=protocol.route,
            reason=reason,
            instruction=instruction,
            llm_result=llm_result,
        )
        return self.enforce_protocol_on_plan(
            plan,
            protocol=protocol,
            reason=reason,
            instruction=instruction,
            llm_result=llm_result,
        )

    def enforce_protocol_on_plan(
        self,
        plan: ExecutionPlan,
        *,
        protocol: RequestProtocol,
        reason: str,
        instruction: str,
        llm_result: dict,
    ) -> ExecutionPlan:
        allowed_steps = self.allowed_steps_for_protocol(protocol)
        filtered_steps = [step for step in plan.steps if step.step_type in allowed_steps]
        required_step = self.required_step_for_protocol(protocol)
        if filtered_steps and any(step.step_type == required_step for step in filtered_steps):
            filtered_steps = self.append_optional_plan_steps_for_protocol(
                filtered_steps,
                protocol=protocol,
                instruction=instruction,
                llm_result=llm_result,
            )
            return ExecutionPlan(goal=plan.goal, primary_intent=protocol.route, steps=filtered_steps)
        return self.build_fallback_plan(
            intent=protocol.route,
            reason=reason,
            instruction=instruction,
            llm_result=llm_result,
        )

    def append_optional_plan_steps_for_protocol(
        self,
        steps: list[PlannerStep],
        *,
        protocol: RequestProtocol,
        instruction: str,
        llm_result: dict,
    ) -> list[PlannerStep]:
        if protocol.route != "doc" or not self.should_include_slides_step("doc", instruction, llm_result):
            return steps
        if any(step.step_type == "generate_slides" for step in steps):
            return steps
        dependency = steps[-1].step_id if steps else None
        return [
            *steps,
            PlannerStep(
                step_id=f"step_{len(steps) + 1}",
                step_type="generate_slides",
                title="基于当前文档生成演示稿",
                depends_on=[dependency] if dependency else [],
            ),
        ]

    def normalize_execution_plan(
        self,
        raw_plan: dict,
        *,
        intent: str,
        instruction: str,
    ) -> ExecutionPlan:
        goal = str(raw_plan.get("goal") or "").strip() or self.default_plan_goal(intent, instruction)
        steps: list[PlannerStep] = []
        raw_steps = raw_plan.get("steps")
        if isinstance(raw_steps, list):
            for index, item in enumerate(raw_steps, start=1):
                if not isinstance(item, dict):
                    continue
                step_type = self.normalize_plan_step_type(str(item.get("type") or item.get("step_type") or "").strip())
                if not step_type:
                    continue
                step_id = str(item.get("id") or item.get("step_id") or f"step_{index}").strip() or f"step_{index}"
                title = str(item.get("title") or "").strip() or self.default_plan_step_title(step_type, intent)
                depends_on = (
                    [str(dep).strip() for dep in item.get("depends_on", []) if str(dep).strip()]
                    if isinstance(item.get("depends_on"), list)
                    else []
                )
                notes = str(item.get("notes") or "").strip() or None
                steps.append(
                    PlannerStep(
                        step_id=step_id,
                        step_type=step_type,
                        title=title,
                        depends_on=depends_on,
                        notes=notes,
                    )
                )
        return ExecutionPlan(goal=goal, primary_intent=intent or "help", steps=steps)

    def build_fallback_plan(
        self,
        *,
        intent: str,
        reason: str,
        instruction: str,
        llm_result: dict,
    ) -> ExecutionPlan:
        primary_intent = intent or "help"
        wants_slides = self.should_include_slides_step(primary_intent, instruction, llm_result)
        if primary_intent in {"summary", "tasks", "risks"}:
            steps = [PlannerStep(step_id="step_1", step_type="analyze_discussion", title="分析讨论并整理结果")]
        elif primary_intent == "doc":
            steps = [PlannerStep(step_id="step_1", step_type="sync_doc", title="生成并同步文档")]
            if wants_slides:
                steps.append(
                    PlannerStep(
                        step_id="step_2",
                        step_type="generate_slides",
                        title="基于当前上下文生成演示稿",
                        depends_on=["step_1"],
                    )
                )
        elif primary_intent == "slides":
            steps = [PlannerStep(step_id="step_1", step_type="generate_slides", title="生成演示稿大纲")]
        elif primary_intent == "canvas":
            steps = [PlannerStep(step_id="step_1", step_type="generate_canvas", title="Generate canvas artifact")]
        elif primary_intent == "status":
            steps = [PlannerStep(step_id="step_1", step_type="answer_status", title="回答当前协作状态")]
        else:
            steps = [PlannerStep(step_id="step_1", step_type="reply_help", title="给出下一步指引")]

        return ExecutionPlan(
            goal=self.default_plan_goal(primary_intent, instruction, reason=reason),
            primary_intent=primary_intent,
            steps=steps,
        )

    @staticmethod
    def normalize_plan_step_type(raw_type: str) -> str:
        normalized = raw_type.lower().strip()
        mapping = {
            "analyze_discussion": "analyze_discussion",
            "analyze": "analyze_discussion",
            "summary": "analyze_discussion",
            "summarize_context": "analyze_discussion",
            "sync_doc": "sync_doc",
            "generate_doc": "sync_doc",
            "write_doc": "sync_doc",
            "doc": "sync_doc",
            "generate_slides": "generate_slides",
            "slides": "generate_slides",
            "presentation": "generate_slides",
            "generate_canvas": "generate_canvas",
            "canvas": "generate_canvas",
            "whiteboard": "generate_canvas",
            "diagram": "generate_canvas",
            "answer_status": "answer_status",
            "status": "answer_status",
            "reply_help": "reply_help",
            "help": "reply_help",
        }
        return mapping.get(normalized, "")

    @staticmethod
    def default_plan_goal(intent: str, instruction: str, *, reason: str | None = None) -> str:
        candidate = " ".join((instruction or "").split()).strip()
        if candidate:
            return candidate[:80]
        if reason:
            return reason[:80]
        return {
            "summary": "总结当前讨论",
            "tasks": "整理任务清单",
            "risks": "识别风险与卡点",
            "doc": "生成并同步协作文档",
            "slides": "生成演示稿",
            "status": "回答当前状态问题",
        }.get(intent, "完成当前协作请求")

    @staticmethod
    def default_plan_step_title(step_type: str, intent: str) -> str:
        return {
            "analyze_discussion": {
                "summary": "总结当前讨论",
                "tasks": "整理任务与待办",
                "risks": "分析风险与卡点",
            }.get(intent, "分析讨论内容"),
            "sync_doc": "生成并同步文档",
            "generate_slides": "生成演示稿",
            "answer_status": "回答状态问题",
            "reply_help": "给出下一步指引",
        }.get(step_type, "执行计划步骤")

    def should_include_slides_step(self, intent: str, instruction: str, llm_result: dict) -> bool:
        if intent == "slides":
            return True
        if intent != "doc":
            return False
        requested_outputs = llm_result.get("requested_outputs")
        if isinstance(requested_outputs, list) and "slides" in {
            str(item).strip().lower() for item in requested_outputs
        }:
            return True
        if isinstance(llm_result.get("slides"), dict) and llm_result.get("slides", {}).get("slides"):
            return True
        return self.is_outline_request(instruction)

    @staticmethod
    def is_outline_request(instruction: str) -> bool:
        return any(keyword in instruction for keyword in ("汇报", "路演", "大纲", "PPT", "ppt", "演示"))

    @staticmethod
    def extract_clarification_request(llm_result: dict) -> dict | None:
        raw = llm_result.get("clarification")
        if not isinstance(raw, dict) or not bool(raw.get("needed")):
            return None

        question = str(raw.get("question") or "").strip()
        if not question:
            return None

        options = (
            [str(item).strip() for item in raw.get("options", []) if str(item).strip()][:4]
            if isinstance(raw.get("options"), list)
            else []
        )
        reason = str(raw.get("reason") or "").strip()
        blocking = raw.get("blocking")
        return {
            "question": question,
            "reason": reason,
            "options": options,
            "blocking": True if blocking is None else bool(blocking),
        }

    def build_plan_artifact(self, *, plan: ExecutionPlan, reason: str, llm_result: dict) -> dict | None:
        next_actions = (
            [str(item).strip() for item in llm_result.get("next_actions", []) if str(item).strip()]
            if isinstance(llm_result.get("next_actions"), list)
            else []
        )
        clarification = self.extract_clarification_request(llm_result)
        preview = {
            "goal": plan.goal,
            "intent": plan.primary_intent or "unknown",
            "operation": llm_result.get("operation") or "",
            "object": llm_result.get("object") or "",
            "route": llm_result.get("route") or plan.primary_intent or "unknown",
            "reason": reason,
            "steps": [
                {
                    "step_id": step.step_id,
                    "step_type": step.step_type,
                    "title": step.title,
                    "depends_on": step.depends_on,
                }
                for step in plan.steps
            ],
            "next_actions": next_actions[:4],
            "task_operation_count": len(llm_result.get("task_operations", []))
            if isinstance(llm_result.get("task_operations"), list)
            else 0,
            "risk_count": len(llm_result.get("risks", []))
            if isinstance(llm_result.get("risks"), list)
            else 0,
        }
        if clarification:
            preview["clarification"] = clarification

        if not any(preview.values()):
            return None

        return {
            "artifact_type": "agent_plan",
            "provider": "llm",
            "title": "Agent 执行规划",
            "status": "needs_confirmation" if clarification and clarification["blocking"] else "ready",
            "preview": preview,
        }
