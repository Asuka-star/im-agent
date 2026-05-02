from __future__ import annotations

from typing import Any

from app.schemas.feishu_event import FeishuMessageContext


class WorkflowCanvasExecution:
    """Builds and persists canvas artifacts for workflow steps."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_canvas_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        canvas = llm_result.get("canvas") if isinstance(llm_result.get("canvas"), dict) else {}
        title = str(canvas.get("title") or workflow._task_run_title(message.text, "canvas")).strip() or "Canvas"
        canvas_tool = workflow._canvas_tool()
        artifact = canvas_tool.generate_flow_artifact(
            title=title,
            instruction=message.text,
            llm_result=llm_result,
            workspace_context=workspace_context,
            task_run_id=task_run_id,
            session_id=message.session_id,
        )
        reply_preview = canvas_tool.format_reply(artifact)
        return {
            "reply_preview": reply_preview,
            "analysis": None,
            "artifacts": [artifact],
            "close_title": str(artifact.get("title") or "canvas"),
        }
