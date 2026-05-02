from __future__ import annotations

from typing import Any

from app.schemas.feishu_event import FeishuMessageContext


class WorkflowAnalysisExecution:
    """Builds discussion analysis responses for workflow steps."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_analysis_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        active_episode_id: int | None,
        intent: str,
    ) -> dict:
        workflow = self.workflow
        source_text = workflow.memory_service.build_discussion_block(
            message.session_id,
            episode_id=active_episode_id,
            exclude_message_id=message.message_id,
        ) or workspace_context or message.text
        analysis = workflow._build_analysis_from_llm(
            session_id=message.session_id,
            source_text=source_text,
            llm_result=llm_result,
            intent=intent,
            reason=str(llm_result.get("reason") or "").strip(),
        )
        reply_preview = workflow.response_formatter.format_analysis_reply(analysis, intent)
        workflow.memory_service.save_round(
            session_id=message.session_id,
            analysis=analysis,
            episode_id=active_episode_id,
            async_embed=True,
            preserve_unmatched_previous=False,
        )
        return {
            "reply_preview": reply_preview,
            "analysis": analysis,
            "artifacts": [],
            "close_title": analysis.summary,
        }
