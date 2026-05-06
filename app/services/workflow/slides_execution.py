from __future__ import annotations

import logging
from typing import Any

from app.schemas.feishu_event import FeishuMessageContext
from app.services.artifact_title_service import ArtifactTitleService

logger = logging.getLogger(__name__)


class WorkflowSlidesExecution:
    """Builds and persists presentation outputs for workflow steps."""

    def __init__(self, workflow: Any) -> None:
        self.workflow = workflow

    def prepare_slides_execution(
        self,
        message: FeishuMessageContext,
        *,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None = None,
    ) -> dict:
        workflow = self.workflow
        package = llm_result.get("slides")
        provider = str(llm_result.get("_slides_provider") or "llm").strip() or "llm"
        if not isinstance(package, dict) or not package.get("slides"):
            try:
                package = workflow.llm_service.generate_presentation_package(workspace_context, message.text)
                llm_result["slides"] = package
                llm_result["_slides_provider"] = "llm"
                provider = "llm"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slide package fallback generation failed: %s", exc)
                package = workflow.fallback_handler.build_fallback_presentation_package(message.session_id)
                llm_result["slides"] = package
                llm_result["_slides_provider"] = "fallback"
                provider = "fallback"
        presentation_tool = workflow._presentation_tool()
        package = self._with_semantic_title(package, instruction=message.text, workspace_context=workspace_context)
        artifact = presentation_tool.persist_artifact(
            package,
            provider=provider,
            session_id=message.session_id,
            task_run_id=task_run_id,
        )
        reply_preview = presentation_tool.format_reply(package, artifact=artifact)
        return {
            "reply_preview": reply_preview,
            "analysis": None,
            "artifacts": [artifact],
            "close_title": "slides",
        }

    def _with_semantic_title(self, package: dict, *, instruction: str, workspace_context: str) -> dict:
        current_title = str(package.get("theme") or "").strip()
        title = ArtifactTitleService.presentation_title(
            current_title=current_title,
            instruction=instruction,
            workspace_context=workspace_context,
        )
        if title:
            package = dict(package)
            package["theme"] = title
        return package
