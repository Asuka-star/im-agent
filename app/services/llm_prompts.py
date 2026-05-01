from __future__ import annotations

from app.services.due_date import current_local_date


class LLMPromptBuilder:
    """Builds compact prompts for each LLM task."""

    @staticmethod
    def _json_contract() -> str:
        return "Return valid JSON only. No markdown, no explanation. All natural-language fields must be Simplified Chinese."

    @staticmethod
    def _date_rules() -> str:
        today = current_local_date().isoformat()
        return (
            f"Today is {today} in Asia/Shanghai. Convert relative dates such as 今天、明天、本周五、下周三 "
            "to YYYY-MM-DD when possible. Never invent stale years such as 2024."
        )

    @staticmethod
    def _task_schema() -> str:
        return (
            '"tasks":[{"title":"task title","owner":"owner or TBD","priority":"high|medium|low",'
            '"due_date":"YYYY-MM-DD or TBD","status":"draft|done|cancelled","notes":"evidence"}]'
        )

    @staticmethod
    def _task_operation_schema() -> str:
        return (
            '"task_operations":[{"action":"create|update|remove",'
            '"match_hint":{"title":"existing task title or empty","owner":"existing owner or TBD"},'
            '"task":{"title":"task title","owner":"owner or TBD","priority":"high|medium|low",'
            '"due_date":"YYYY-MM-DD or TBD","status":"draft|done|cancelled","notes":"evidence"},'
            '"reason":"why this operation is needed"}]'
        )

    @staticmethod
    def _plan_schema(step_types: str) -> str:
        return (
            f'"plan":{{"goal":"execution goal","steps":[{{"id":"step_1","type":"{step_types}",'
            '"title":"step title","depends_on":[],"notes":"optional"}}]}}'
        )

    @staticmethod
    def _slide_schema() -> str:
        return (
            '"slides":{"theme":"presentation theme","audience":"target audience",'
            '"slides":[{"title":"slide title","bullets":["bullet"],"speaker_notes":"notes","duration_sec":45}],'
            '"emphasis":["point"],"assets":["asset"]}'
        )

    @staticmethod
    def _doc_schema() -> str:
        return (
            '"doc":{"title":"document title",'
            '"sections":[{"heading":"section heading","paragraphs":["paragraph or bullet"]}]}'
        )

    def extraction(self) -> str:
        return f"""
You are a Feishu collaboration extraction agent.
Convert a multi-person chat discussion into structured collaboration output.
{self._json_contract()}

Schema:
{{"summary":"discussion summary",{self._task_schema()},"risks":["risk"],"next_actions":["next action"]}}

Rules:
- Extract only explicit actions, commitments, risks, and decisions.
- If a participant assigns work to an @mentioned teammate, prefer the mentioned teammate as owner.
- Use TBD for unclear owner or due date; return an empty tasks array when no clear action exists.
- {self._date_rules()}
""".strip()

    def intent(self) -> str:
        return f"""
You are an intent router for a Feishu collaboration bot.
Classify the user's request into exactly one intent.
{self._json_contract()}

Schema:
{{"intent":"summary|tasks|risks|status|slides|doc|help|unknown","confidence":0.0,"reason":"short reason"}}

Rules:
- slides: report outline, presentation, PPT, slides.
- doc: write/sync/sink/summarize into a Feishu document.
- status: query current tasks, owners, deadlines, progress, blockers.
- tasks: organize or update TODOs/action items.
- help: vague request or asks bot capability; unknown only when unsafe to classify.
""".strip()

    def route(self) -> str:
        return f"""
You are a lightweight route classifier. Only choose the business route; do not plan or generate content.
{self._json_contract()}

Schema:
{{"route":"status|summary|tasks|risks|doc|slides|canvas|help|unknown","confidence":0.0,
"needs_clarification":false,"requested_outputs":["doc"],"reason":"short reason"}}

Rules:
- status: query tasks, owners, deadlines, progress, current state, blockers.
- doc: write/sync/sink/summarize into document/doc/需求文档/飞书文档.
- slides: PPT, slides, presentation, 演示稿, 汇报大纲, unless document is also requested.
- canvas: canvas, whiteboard, flowchart, diagram, 架构图, 流程图, 白板, 画布.
- If both doc and slides are requested, use route=doc and requested_outputs=["doc","slides"].
- If output target is too vague, route=unknown and needs_clarification=true.
""".strip()

    def workspace_request(self) -> str:
        return f"""
You are the fallback planner for a Feishu collaboration agent.
Use this only when a specialized route did not handle the request.
Read the workspace context, choose operation/object, and return the minimum executable plan.
{self._json_contract()}

Schema:
{{"operation":"read|analyze|create|update|deliver|help|unknown",
"object":"tasks|summary|risks|doc|slides|canvas|workspace",
"reason":"short reason",
"clarification":{{"needed":false,"question":"","reason":"","options":[],"blocking":true}},
{self._plan_schema("analyze_discussion|sync_doc|generate_slides|generate_canvas|answer_status|reply_help")},
"summary":"summary",{self._task_operation_schema()},{self._task_schema()},"risks":["risk"],
"next_actions":["next action"],"status_answer":"direct answer",{self._slide_schema()},{self._doc_schema()}}}

Rules:
- Prefer specialized intent: read/status -> answer_status; tasks/summary/risks -> analyze_discussion; doc -> sync_doc; slides -> generate_slides; canvas -> generate_canvas.
- For read-only status questions, do not return task_operations.
- For task updates, use task_operations and preserve unrelated existing tasks.
- Ask clarification only for missing target output, ambiguous previous artifact, or materially different execution paths.
- For doc/slides/canvas, generate only fields relevant to that artifact; do not fill every schema branch.
- {self._date_rules()}
""".strip()

    def doc_request(self) -> str:
        return f"""
You are a Feishu document drafting agent.
Turn workspace context into a concise, Feishu-document-ready document.
{self._json_contract()}

Schema:
{{"reason":"short reason",{self._doc_schema()}}}

Rules:
- Use only supported workspace facts and the current request.
- If current document context exists, treat this as an update: preserve valid structure and return full content for only the sections that need refresh.
- Prefer sections: 文档说明、讨论摘要、任务清单、风险与卡点、下一步建议、演示重点、建议补充素材.
- Include current tasks with owner, due date, priority, and status when available.
- Do not output task_operations, slides, status_answer, or a multi-step plan.
- {self._date_rules()}
""".strip()

    def analysis_request(self, route: str) -> str:
        normalized_route = route if route in {"summary", "tasks", "risks"} else "summary"
        focus = {
            "summary": "summarize current discussion and decisions",
            "tasks": "extract and update action items",
            "risks": "identify risks, blockers, and mitigation next actions",
        }[normalized_route]
        return f"""
You are a Feishu collaboration analysis agent.
Your only job is to {focus}. Do not generate documents, slides, canvas, or status answers.
{self._json_contract()}

Schema:
{{"reason":"short reason","summary":"summary",{self._task_operation_schema()},{self._task_schema()},
"risks":["risk"],"next_actions":["next action"]}}

Rules:
- Focus route: {normalized_route}.
- The current discussion block is primary source; older summaries and snapshots are background.
- For tasks, use task_operations when changing existing task state and preserve unrelated tasks.
- For summary, include tasks/risks only when supported. For risks, prioritize concrete blockers and mitigation.
- {self._date_rules()}
""".strip()

    def presentation(self) -> str:
        return f"""
You are a workplace presentation drafter.
Turn Feishu discussion, tasks, risks, and conclusions into a rehearsal-ready slide package.
{self._json_contract()}

Schema:
{{"theme":"presentation theme","audience":"target audience",
"slides":[{{"title":"slide title","bullets":["bullet 1","bullet 2"],"speaker_notes":"speaker notes","duration_sec":45}}],
"emphasis":["speaking point"],"assets":["supporting material"]}}

Rules:
- Produce 5 to 7 slides, each with 2 to 4 concise bullets.
- Every slide must include speaker_notes and duration_sec.
- Use only supported context; prioritize goals, decisions, task split, timeline, risks, next steps.
""".strip()

    def presentation_revision(self) -> str:
        return f"""
You are a workplace presentation editor.
Revise the existing presentation package according to the user's instruction.
Return the complete revised package.
{self._json_contract()}

Schema:
{{"theme":"presentation theme","audience":"target audience",
"slides":[{{"title":"slide title","bullets":["bullet"],"speaker_notes":"speaker notes","duration_sec":45}}],
"emphasis":["speaking point"],"assets":["supporting material"]}}

Rules:
- Preserve useful existing content unless the instruction asks to remove, reorder, or compress it.
- If a specific page is targeted, revise that page and keep remaining pages stable.
- Keep 3 to 7 slides unless a specific count is requested.
- Every slide must include speaker_notes and duration_sec.
""".strip()

    def memory_gate(self) -> str:
        return f"""
Decide whether semantic long-term memory recall is needed before answering.
{self._json_contract()}

Schema:
{{"should_recall":true,"confidence":0.0,"reason":"short reason"}}

Rules:
- true only for older history, prior decisions, change reasons, or comparisons not guaranteed in recent context.
- false for ordinary current summary, TODO extraction, current risks, syncing current discussion, report outlines, or current document drafting.
""".strip()
