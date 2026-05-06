import unittest
from unittest.mock import patch

from app.services.llm_client import OpenAICompatibleJSONClient
from app.services.llm import LLMService
from app.services.llm_prompts import LLMPromptBuilder


class LLMPromptTests(unittest.TestCase):
    def test_workspace_prompt_is_compact_and_canvas_aware(self) -> None:
        prompt = LLMPromptBuilder().workspace_request()

        self.assertLess(len(prompt), 5000)
        self.assertIn("generate_canvas", prompt)
        self.assertIn("Return valid JSON only", prompt)
        self.assertIn("Simplified Chinese", prompt)

    def test_prompts_do_not_contain_common_mojibake_markers(self) -> None:
        prompts = [
            LLMPromptBuilder().route(),
            LLMPromptBuilder().doc_request(),
            LLMPromptBuilder().requirement_brief(),
            LLMPromptBuilder().analysis_request("tasks"),
            LLMPromptBuilder().presentation(),
        ]

        for prompt in prompts:
            self.assertNotIn("�", prompt)
            self.assertNotIn("鈥", prompt)
            self.assertNotIn("鍙", prompt)

    def test_doc_prompt_keeps_tasks_subordinate_to_requirement_document(self) -> None:
        prompt = LLMPromptBuilder().doc_request()

        self.assertIn("需求文档", prompt)
        self.assertIn("背景与痛点", prompt)
        self.assertIn("产品流程", prompt)
        self.assertIn("实施计划与分工", prompt)
        self.assertIn("never let the whole document become a task list", prompt)
        self.assertIn("IM discussion block as primary requirement evidence", prompt)
        self.assertIn("Do not write placeholder content", prompt)
        self.assertIn("do not invent dates, date ranges, owners, teams, or assignees", prompt)
        self.assertIn("Do not invent technology stacks", prompt)
        self.assertIn("frameworks, databases", prompt)

    def test_requirement_brief_prompt_separates_requirement_facts_from_tasks(self) -> None:
        prompt = LLMPromptBuilder().requirement_brief()

        self.assertIn("requirement-brief extraction agent", prompt)
        self.assertIn('"requirement_brief"', prompt)
        self.assertIn("first-class requirement evidence", prompt)
        self.assertIn("implementation_plan optional and subordinate", prompt)
        self.assertIn("Never infer implementation_plan dates", prompt)
        self.assertIn("Do not convert", prompt)

    def test_extraction_prompt_does_not_turn_requirement_discovery_into_tasks(self) -> None:
        prompt = LLMPromptBuilder().extraction()

        self.assertIn("narrow Feishu task extraction agent", prompt)
        self.assertIn("Product ideas, target users, pain points", prompt)
        self.assertIn("keep tasks empty", prompt)
        self.assertIn("unless there is an owner, deadline, or explicit execution command", prompt)

    def test_presentation_prompt_prefers_formal_solution_arc(self) -> None:
        prompt = LLMPromptBuilder().presentation()

        self.assertIn("formal rehearsal-ready presentation", prompt)
        self.assertIn("背景痛点 -> 目标用户/核心需求 -> 产品流程", prompt)
        self.assertIn("do not make the deck read like a task assignment report", prompt)

    def test_route_prompt_defaults_discussion_settling_to_doc(self) -> None:
        prompt = LLMPromptBuilder().route()

        self.assertIn("整理这轮讨论", prompt)
        self.assertIn('requested_outputs ["doc"]', prompt)

    def test_workspace_command_prompt_treats_tasks_as_implementation_section(self) -> None:
        prompt = LLMPromptBuilder().workspace_command()

        self.assertIn("requested_outputs=[\"doc\"]", prompt)
        self.assertIn("not the command goal", prompt)
        self.assertIn("Primary product lifecycle", prompt)
        self.assertIn("route=doc", prompt)

    def test_workspace_request_prompt_keeps_requirement_artifacts_primary(self) -> None:
        prompt = LLMPromptBuilder().workspace_request()

        self.assertIn("background, pain points, target users", prompt)
        self.assertIn("Do not use task snapshots as the main content", prompt)

    def test_llm_service_keeps_prompt_wrapper_methods(self) -> None:
        service = LLMService()

        self.assertEqual(service._route_prompt(), service.prompts.route())
        self.assertEqual(service._workspace_command_prompt(), service.prompts.workspace_command())
        self.assertEqual(service._dag_plan_prompt(), service.prompts.dag_plan())
        self.assertEqual(service._analysis_request_prompt("risks"), service.prompts.analysis_request("risks"))
        self.assertEqual(service._requirement_brief_prompt(), service.prompts.requirement_brief())
        self.assertEqual(service._doc_edit_intent_prompt(), service.prompts.doc_edit_intent())
        self.assertEqual(service._next_action_rerank_prompt(), service.prompts.next_action_rerank())

    def test_dag_plan_prompt_is_planning_only_and_bounded(self) -> None:
        prompt = LLMPromptBuilder().dag_plan()

        self.assertLess(len(prompt), 3500)
        self.assertIn("lightweight DAG planner", prompt)
        self.assertIn("Do not draft document text", prompt)
        self.assertIn("sync_doc|generate_slides|generate_canvas", prompt)
        self.assertIn("requested_outputs", prompt)
        self.assertIn("Do not include content payload keys", prompt)

    def test_doc_edit_intent_prompt_is_contract_only(self) -> None:
        prompt = LLMPromptBuilder().doc_edit_intent()

        self.assertIn("Only parse the user's requested document mutation", prompt)
        self.assertIn("Do not draft document content", prompt)
        self.assertIn('"artifact_edit_plan"', prompt)
        self.assertIn('"include_anchor"', prompt)
        self.assertIn('"confirmation"', prompt)
        self.assertNotIn('"doc":{"title"', prompt)

    def test_route_prompt_describes_compound_artifact_requests(self) -> None:
        prompt = LLMPromptBuilder().route()

        self.assertIn("Compound requests", prompt)
        self.assertIn("analysis intent", prompt)
        self.assertIn("requested_outputs", prompt)
        self.assertIn("Do not include content payload keys", prompt)

    def test_workspace_command_prompt_separates_task_delete_from_ppt_generation(self) -> None:
        prompt = LLMPromptBuilder().workspace_command()

        self.assertIn("Command Interpreter Agent", prompt)
        self.assertIn('"route":"status|tasks|summary|risks|doc|slides|canvas|delivery|help|unknown"', prompt)
        self.assertIn('"operation":"read|analyze|create|update|remove|complete|assign|generate|revise|recommend|chat|clarify|help|unknown"', prompt)
        self.assertIn('"target_text"', prompt)
        self.assertIn('"artifact_goals"', prompt)
        self.assertIn("Risk/blocker analysis", prompt)
        self.assertIn("删除制作ppt的任务", prompt)
        self.assertIn("does not mean generate slides", prompt)
        self.assertIn("Do not execute tasks", prompt)

    def test_interpret_workspace_command_uses_graph_timeout(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(service, "_chat_json", return_value={"operation": "read", "object": "tasks"}) as chat_json:
            result = service.interpret_workspace_command("[tasks]", "现在有哪些任务")

        self.assertEqual(result["operation"], "read")
        self.assertEqual(chat_json.call_args.kwargs["request_name"], "interpret_workspace_command")

    def test_resolve_requirement_brief_uses_dedicated_request_name(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={"requirement_brief": {"title": "校园活动报名系统", "goals": ["统一报名审核"]}},
        ) as chat_json:
            result = service.resolve_requirement_brief("[讨论]", "整理成需求方案文档")

        self.assertEqual(result["route"], "doc")
        self.assertEqual(result["object"], "doc")
        self.assertEqual(result["requirement_brief"]["title"], "校园活动报名系统")
        self.assertEqual(chat_json.call_args.kwargs["request_name"], "resolve_requirement_brief")

    def test_resolve_doc_request_removes_ungrounded_plan_dates_and_owners(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        workspace_context = "\n".join(
            [
                "[协作上下文]",
                "[本次待同步讨论]",
                "- 发言人: u1 | 内容: 补充一下，评委更关心这个系统为什么适合 IM 协作场景。",
                "- 发言人: u1 | 内容: 技术方案里可以强调：飞书 IM 负责入口，Agent 负责理解和规划，Workbench 负责展示执行过程，文档和 PPT 是最终交付物。",
                "[当前协作文档]",
                "标题：校园活动报名系统需求方案 - 统计至2026-05-06 15:00",
                "- 实施计划与分工",
                "  - 具体任务分工、截止时间及阻塞项待后续会议确定。",
            ]
        )

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "doc": {
                    "title": "校园活动报名系统需求方案",
                    "sections": [
                        {
                            "heading": "技术方案",
                            "paragraphs": ["飞书 IM 负责入口，Agent 负责理解和规划。"],
                        },
                        {
                            "heading": "实施计划与分工",
                            "paragraphs": [
                                "一期实施计划（2026年5月-6月）：",
                                "- 需求确认与设计：2026-05-06至2026-05-15，负责人：Zeleous",
                                "- 核心功能开发：2026-05-16至2026-06-15，负责人：开发团队",
                                "二期规划：根据一期反馈，规划数据看板和候补队列功能。",
                            ],
                        },
                    ],
                }
            },
        ):
            result = service.resolve_doc_request(workspace_context, "更新刚才的需求方案文档，加入 IM 协作场景价值和技术架构说明")

        sections = result["doc"]["sections"]
        plan_section = next(section for section in sections if section["heading"] == "实施计划与分工")
        content = "\n".join(plan_section["paragraphs"])
        self.assertIn("尚未在本轮讨论中明确", content)
        self.assertIn("二期规划", content)
        self.assertNotIn("2026-05-15", content)
        self.assertNotIn("Zeleous", content)
        self.assertNotIn("开发团队", content)

    def test_resolve_doc_request_removes_ungrounded_tech_stack(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        workspace_context = "\n".join(
            [
                "[当前需求工作区]",
                "- 标题: 校园活动报名与审核系统",
                "[当前需求讨论事实]",
                "- 学生要快速报名，负责人要审核名单，老师要查看活动数据和风险。",
                "- 核心流程是学生报名、负责人审核、老师查看统计结果。",
            ]
        )

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "doc": {
                    "title": "校园活动报名与审核系统需求方案",
                    "sections": [
                        {
                            "heading": "技术方案",
                            "paragraphs": [
                                "整体采用前后端分离架构，前端使用 Vue.js 或 React，后端使用 Spring Boot 或 Node.js，数据库采用 MySQL 或 PostgreSQL。",
                                "报名审核模块需要支持报名提交、审核流转和统计结果查看。",
                            ],
                        }
                    ],
                }
            },
        ):
            result = service.resolve_doc_request(workspace_context, "整理成正式需求方案文档")

        content = "\n".join(result["doc"]["sections"][0]["paragraphs"])
        self.assertIn("技术栈、数据库、部署方式和系统集成方案尚未", content)
        self.assertIn("报名审核模块", content)
        self.assertNotIn("Vue.js", content)
        self.assertNotIn("Spring Boot", content)
        self.assertNotIn("MySQL", content)

    def test_resolve_doc_request_keeps_tech_stack_grounded_in_current_document(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        workspace_context = "\n".join(
            [
                "[当前协作文档]",
                "标题：校园活动报名与审核系统需求方案",
                "章节快照：",
                "- 技术方案",
                "  - 前端使用 Vue.js，数据库采用 MySQL。",
            ]
        )

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "doc": {
                    "title": "校园活动报名与审核系统需求方案",
                    "sections": [
                        {
                            "heading": "技术方案",
                            "paragraphs": ["前端使用 Vue.js，数据库采用 MySQL。"],
                        }
                    ],
                }
            },
        ):
            result = service.resolve_doc_request(workspace_context, "更新当前需求方案文档")

        content = "\n".join(result["doc"]["sections"][0]["paragraphs"])
        self.assertIn("Vue.js", content)
        self.assertIn("MySQL", content)
        self.assertNotIn("尚未在当前需求讨论或文档中明确", content)

    def test_next_action_rerank_prompt_is_dedicated_and_bounded(self) -> None:
        prompt = LLMPromptBuilder().next_action_rerank()

        self.assertIn("rule-generated candidates", prompt)
        self.assertIn("Do not invent new actions", prompt)
        self.assertIn('"order"', prompt)
        self.assertIn("action_id values already present", prompt)
        self.assertNotIn('"plan"', prompt)

    def test_llm_service_builds_standard_json_payload(self) -> None:
        service = LLMService()
        service.model = "demo-model"

        payload = service._json_payload(system_prompt="sys", user_content="user", temperature=0.1)

        self.assertEqual(payload["model"], "demo-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "user"})

    def test_llm_service_can_use_xiaomi_as_primary_provider(self) -> None:
        service = LLMService()
        service.api_key = ""
        service.base_url = ""
        service.model = ""
        service.xiaomi_api_key = "xiaomi-key"
        service.xiaomi_base_url = "https://xiaomi.example/v1"
        service.xiaomi_model = "xiaomi-model"

        self.assertTrue(service.is_configured())
        self.assertEqual([provider.name for provider in service._configured_providers()], ["xiaomi"])

    def test_llm_service_normalizes_xiaomi_model_aliases(self) -> None:
        service = LLMService()
        service.api_key = ""
        service.base_url = ""
        service.model = ""
        service.xiaomi_api_key = "xiaomi-key"
        service.xiaomi_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
        service.xiaomi_model = "MiMo-V2.5-Pro"

        providers = service._configured_providers()

        self.assertEqual(providers[0].model, "mimo-v2.5-pro")

    def test_llm_service_ignores_whitespace_only_provider_values(self) -> None:
        service = LLMService()
        service.api_key = "deepseek-key"
        service.base_url = "https://openrouter.example/v1"
        service.model = "deepseek-model"
        service.xiaomi_api_key = " "
        service.xiaomi_base_url = "https://xiaomi.example/v1"
        service.xiaomi_model = "MiMo-V2.5-Pro"

        self.assertEqual([provider.name for provider in service._configured_providers()], ["deepseek"])

    def test_llm_service_falls_back_from_xiaomi_to_deepseek(self) -> None:
        service = LLMService()
        service.xiaomi_api_key = "xiaomi-key"
        service.xiaomi_base_url = "https://xiaomi.example/v1"
        service.xiaomi_model = "xiaomi-model"
        service.api_key = "deepseek-key"
        service.base_url = "https://openrouter.example/v1"
        service.model = "deepseek-model"

        class FakeProviderClient:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def chat_json(self, payload, *, request_name, timeout_seconds):
                self.calls.append(
                    {
                        "payload": payload,
                        "request_name": request_name,
                        "timeout_seconds": timeout_seconds,
                    }
                )
                if isinstance(self.response, Exception):
                    raise self.response
                return dict(self.response)

        clients = {
            "xiaomi": FakeProviderClient(RuntimeError("xiaomi unavailable")),
            "deepseek": FakeProviderClient({"ok": True}),
        }

        with patch.object(service, "_client_for_provider", side_effect=lambda provider: clients[provider.name]):
            result = service._chat_json(
                {"model": "placeholder", "messages": []},
                request_name="route_workspace_request",
                timeout_seconds=3,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(clients["xiaomi"].calls[0]["payload"]["model"], "xiaomi-model")
        self.assertEqual(clients["deepseek"].calls[0]["payload"]["model"], "deepseek-model")

    def test_llm_service_retries_xiaomi_without_response_format_before_fallback(self) -> None:
        service = LLMService()
        service.xiaomi_api_key = "xiaomi-key"
        service.xiaomi_base_url = "https://xiaomi.example/v1"
        service.xiaomi_model = "MiMo-V2.5-Pro"
        service.api_key = "deepseek-key"
        service.base_url = "https://openrouter.example/v1"
        service.model = "deepseek-model"

        class XiaomiClient:
            def __init__(self):
                self.calls = []

            def chat_json(self, payload, *, request_name, timeout_seconds):
                self.calls.append(payload)
                if "response_format" in payload:
                    raise RuntimeError("response_format unsupported")
                return {"ok": True, "provider": "xiaomi"}

        xiaomi = XiaomiClient()
        deepseek = object()

        with patch.object(service, "_client_for_provider", side_effect=lambda provider: xiaomi if provider.name == "xiaomi" else deepseek):
            result = service._chat_json(
                {"model": "placeholder", "response_format": {"type": "json_object"}, "messages": []},
                request_name="route_workspace_request",
                timeout_seconds=3,
            )

        self.assertEqual(result["provider"], "xiaomi")
        self.assertEqual(xiaomi.calls[0]["model"], "mimo-v2.5-pro")
        self.assertIn("response_format", xiaomi.calls[0])
        self.assertNotIn("response_format", xiaomi.calls[1])

    def test_plan_workspace_request_uses_lightweight_timeout_and_cache(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        result = {
            "operation": "create",
            "object": "slides",
            "confidence": 0.82,
            "requested_outputs": ["slides", "canvas"],
            "plan": {"steps": [{"id": "step_1", "type": "generate_slides"}]},
        }

        with patch.object(service, "_chat_json", return_value=result) as chat_json:
            first = service.plan_workspace_request("[workspace]", "做汇报材料并画流程图")
            first["object"] = "workspace"
            first["requested_outputs"].append("doc")
            second = service.plan_workspace_request("[workspace]", "做汇报材料并画流程图")

        self.assertEqual(first["object"], "workspace")
        self.assertEqual(second["object"], "slides")
        self.assertEqual(second["requested_outputs"], ["slides", "canvas"])
        chat_json.assert_called_once()
        self.assertEqual(chat_json.call_args.kwargs["request_name"], "plan_workspace_request")

    def test_lightweight_plan_strips_generated_content_payloads(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        result = {
            "action": "create",
            "target": "slides",
            "confidence": 0.82,
            "requested_outputs": ["presentation", "diagram", "slides", "deck"],
            "plan": {
                "goal": "生成汇报",
                "steps": [
                    {
                        "id": "step_1",
                        "type": "presentation",
                        "title": "生成汇报材料",
                        "depends_on": [None, "", "step_0"],
                        "payload": {"slides": ["should be dropped"]},
                    },
                    {"id": "step_2", "type": "write_full_deck", "title": "非法步骤"},
                ],
            },
            "slides": {"slides": [{"title": "不该被轻量规划结果携带"}]},
            "doc": {"title": "不该被轻量规划结果携带"},
            "tasks": [{"title": "不该被轻量规划结果携带"}],
            "summary": "不该被轻量规划结果携带",
            "next_actions": ["不该被轻量规划结果携带"],
        }

        with patch.object(service, "_chat_json", return_value=result):
            planned = service.plan_workspace_request("[workspace]", "生成汇报材料")

        self.assertEqual(planned["requested_outputs"], ["slides", "canvas"])
        self.assertEqual(planned["operation"], "create")
        self.assertEqual(planned["object"], "slides")
        self.assertEqual(planned["plan"]["steps"], [
            {
                "id": "step_1",
                "type": "generate_slides",
                "title": "生成汇报材料",
                "depends_on": [],
            }
        ])
        self.assertNotIn("slides", planned)
        self.assertNotIn("doc", planned)
        self.assertNotIn("tasks", planned)
        self.assertNotIn("summary", planned)
        self.assertNotIn("next_actions", planned)

    def test_lightweight_route_strips_non_route_payloads(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "route": "presentation",
                "confidence": 0.91,
                "requested_outputs": ["presentation", "feishu_doc"],
                "reason": "用户需要汇报材料",
                "slides": {"slides": [{"title": "不该保留"}]},
            },
        ):
            routed = service.route_workspace_request("生成项目汇报 PPT")

        self.assertEqual(routed["requested_outputs"], ["slides", "doc"])
        self.assertNotIn("slides", routed)

    def test_lightweight_route_normalizes_confidence_and_clarification(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "route": "status",
                "confidence": -1,
                "needs_clarification": -1,
            },
        ):
            routed = service.route_workspace_request("帮我总结一下项目进展")

        self.assertEqual(routed["confidence"], 0.0)
        self.assertFalse(routed["needs_clarification"])

    def test_lightweight_sanitizers_tolerate_scalar_outputs_and_dropped_dependencies(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(
            service,
            "_chat_json",
            return_value={
                "operation": "create",
                "object": "canvas",
                "requested_outputs": "flowchart",
                "plan": {
                    "steps": [
                        {"id": "step_1", "type": "generate_canvas", "depends_on": ["step_2", "step_1", "missing"]},
                        {"id": "step_2", "type": "write_full_deck"},
                    ]
                },
            },
        ):
            planned = service.plan_workspace_request("[workspace]", "画流程图")

        self.assertEqual(planned["requested_outputs"], ["canvas"])
        self.assertEqual(planned["plan"]["steps"], [
            {"id": "step_1", "type": "generate_canvas", "title": "", "depends_on": []}
        ])

    def test_llm_service_context_wrapper_uses_clean_chinese(self) -> None:
        content = LLMService._context_request_content("上下文", "生成文档")

        self.assertIn("[工作区上下文]", content)
        self.assertIn("[当前请求]", content)
        self.assertNotIn("ç", content)

    def test_doc_edit_intent_heuristic_detects_range_mutations(self) -> None:
        self.assertTrue(
            LLMService._instruction_needs_doc_edit_intent(
                "将文档里“后续计划”后面的内容全部删除",
                "[当前协作文档]\n后续计划\nUpdate",
            )
        )
        self.assertFalse(LLMService._instruction_needs_doc_edit_intent("帮我总结一下最近任务", ""))

    def test_resolve_doc_request_merges_dedicated_edit_plan(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"
        edit_plan = {
            "artifact_type": "doc",
            "mutation_required": True,
            "scope": "targeted",
            "ops": [
                {
                    "type": "delete",
                    "target": {"kind": "heading", "query": "后续计划", "scope": "after", "include_anchor": False},
                    "payload": {},
                    "reason": "删除锚点后的内容",
                }
            ],
        }

        with patch.object(service, "resolve_doc_edit_intent", return_value={"reason": "解析编辑意图", "artifact_edit_plan": edit_plan}):
            with patch.object(service, "_chat_json", return_value={"doc": {"title": "项目分工", "sections": []}}):
                result = service.resolve_doc_request("[当前协作文档]\n后续计划", "将文档里“后续计划”后面的内容全部删除")

        self.assertIs(result["artifact_edit_plan"], edit_plan)
        self.assertEqual(result["route"], "doc")
        self.assertEqual(result["object"], "doc")

    def test_resolve_doc_request_skips_edit_intent_for_plain_draft(self) -> None:
        service = LLMService()
        service.api_key = "test-key"
        service.base_url = "https://example.test"
        service.model = "demo-model"

        with patch.object(service, "resolve_doc_edit_intent") as edit_intent:
            with patch.object(service, "_chat_json", return_value={"doc": {"title": "项目分工", "sections": []}}):
                service.resolve_doc_request("近期任务", "帮我总结一下最近任务并写成文档")

        edit_intent.assert_not_called()

    def test_llm_client_parses_fenced_json(self) -> None:
        parsed = OpenAICompatibleJSONClient.parse_json('```json\n{"ok": true}\n```')

        self.assertEqual(parsed, {"ok": True})

    def test_llm_client_rejects_missing_text(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing choices"):
            OpenAICompatibleJSONClient.extract_text({"choices": []})


if __name__ == "__main__":
    unittest.main()
