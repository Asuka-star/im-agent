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

    @staticmethod
    def _artifact_edit_plan_schema() -> str:
        return (
            '"artifact_edit_plan":{"artifact_type":"doc|slides|canvas","mutation_required":false,'
            '"scope":"all|targeted","ops":[{"type":"rewrite|delete|append|rename|reorder|compress|update|format|move",'
            '"target":{"kind":"heading|slide|node|anchor_range|instruction","query":"target text","queries":["optional"],'
            '"anchor":"anchor text","scope":"exact|section|group|after|before|between|all",'
            '"include_anchor":false,"stop_at":"optional end anchor"},'
            '"payload":{},"reason":"why this mutation is needed"}],'
            '"confirmation":{"required":false,"reason":"","question":""},"fallback":"ask_clarification"}'
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
- For Chinese patterns like "王五来做产品经理" or "王五负责/协调/推进 X", owner is 王五 and the task title is the work after the action, not the sender.
- If one person is coordinating several domains, keep it as one coordination task unless separate owners or deadlines are stated.
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
- Compound requests may combine an analysis intent with an output artifact, such as "summarize tasks and put it in a document" or "extract risks and make slides"; choose the requested artifact route as primary and keep all artifacts in requested_outputs.
- If multiple artifacts are requested, keep all of them in requested_outputs. Prefer route=doc when doc is included, otherwise route=slides when slides is included, otherwise route=canvas.
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
- For create/update of doc/slides/canvas, include {self._artifact_edit_plan_schema()} when the user asks to mutate an existing artifact.
- {self._date_rules()}
""".strip()

    def doc_request(self) -> str:
        return f"""
You are a Feishu document drafting agent.
Turn workspace context into concise, Feishu-document-ready content.
{self._json_contract()}

Schema:
{{"reason":"short reason",{self._doc_schema()},{self._artifact_edit_plan_schema()}}}

Rules:
- Use only supported workspace facts and the current request.
- If current document context exists, treat this as an update: preserve valid structure and return full content for only the sections that need refresh.
- Focus on document content. Do not infer destructive ranges from generated sections alone.
- If the current request clearly contains document edit operations, include a minimal artifact_edit_plan, but keep mutation semantics separate from content drafting.
- Prefer sections: 文档说明、讨论摘要、任务清单、风险与卡点、下一步建议、演示重点、建议补充素材.
- Include current tasks with owner, due date, priority, and status when available.
- Do not output task_operations, slides, status_answer, or a multi-step plan.
- {self._date_rules()}
""".strip()

    def doc_edit_intent(self) -> str:
        return f"""
You are a document edit-intent parser for a Feishu document tool.
Only parse the user's requested document mutation. Do not draft document content, summarize discussion, or create sections.
{self._json_contract()}

Schema:
{{"reason":"short reason",{self._artifact_edit_plan_schema()}}}

Rules:
- Return artifact_type=doc.
- If the request is not asking to mutate an existing document, set mutation_required=false and ops=[].
- Preserve every independent edit operation in ops instead of collapsing them into one generic update.
- For delete/update/rewrite/rename/format/move/append requests, set mutation_required=true and describe the operation precisely.
- For range wording such as "后面/之后/以下/below/after", use target.scope="after" and include_anchor=false unless the user says to include the anchor.
- For "这一组/本组/整个小节", use target.scope="group" and keep the visible heading or anchor text in target.query.
- For "之间/between", set target.scope="between", target.anchor as the start anchor, and target.stop_at as the end anchor.
- If the target cannot be grounded to a current heading, quote the user's target text in target.query and set fallback=ask_clarification only when executing it would be unsafe.
- For broad destructive edits such as deleting all content or clearing the whole document, set confirmation.required=true with a concise Chinese question.
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
{{"artifact_edit_plan":{{"artifact_type":"slides","mutation_required":true,
"scope":"all|targeted","ops":[{{"type":"rewrite|delete|append|rename|reorder|compress|update",
"target":{{"kind":"slide|title|instruction","query":"P2 or slide title","queries":["optional"]}},
"payload":{{}},"reason":"why"}}],"fallback":"ask_clarification"}},
"package":{{"theme":"presentation theme","audience":"target audience",
"slides":[{{"title":"slide title","bullets":["bullet"],"speaker_notes":"speaker notes","duration_sec":45}}],
"emphasis":["speaking point"],"assets":["supporting material"]}}}}

Rules:
- Always set artifact_edit_plan.mutation_required=true for any requested edit; never rely on content diff alone.
- Preserve useful existing content unless the instruction asks to remove, reorder, or compress it.
- If a specific page is targeted, revise that page and keep remaining pages stable.
- If the target page/content is ambiguous, return artifact_edit_plan with fallback=ask_clarification and a question.
- Keep 3 to 7 slides unless a specific count is requested.
- Every slide must include speaker_notes and duration_sec.
""".strip()

    def next_action_rerank(self) -> str:
        return f"""
You are a next-action recommendation editor for a Feishu collaboration agent.
You only rerank and lightly rewrite rule-generated candidates. Do not invent new actions.
{self._json_contract()}

Schema:
{{"order":["existing_action_id"],
"recommendations":[{{"action_id":"existing_action_id","title":"short Chinese title",
"description":"optional one sentence","reason":"why this is the best next step",
"command":"safe user-confirmed command text"}}]}}

Rules:
- Use only action_id values already present in the input recommendations.
- Keep every action_type, target_kind, target_id, priority, and safety boundary unchanged.
- Prefer actions that unblock the current task, fill missing artifacts, or recover failures.
- Never recommend destructive edits unless the candidate already requires confirmation.
- Return at most the requested max_items.
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
