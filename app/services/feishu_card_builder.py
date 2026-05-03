from __future__ import annotations

from app.core.config import settings
from app.services.cards.builders import FeishuCardBuilder


class FeishuArtifactCardBuilder:
    """Backward-compatible wrapper around the structured Feishu card builder."""

    def __init__(self) -> None:
        self._builder = FeishuCardBuilder()

    def build_artifact_card(
        self,
        *,
        title: str,
        mode: str,
        artifacts: list[dict] | None,
        summary: str | None = None,
        task_run_id: str | None = None,
        source_message_id: str | None = None,
        session_id: str | None = None,
        checks: list[dict] | None = None,
    ) -> dict | None:
        return self._builder.build_artifact_delivery_card(
            title=title,
            mode=mode,
            artifacts=artifacts,
            summary=summary,
            task_run_id=task_run_id,
            source_message_id=source_message_id,
            session_id=session_id,
            checks=checks,
        )

    def build_task_confirmation_card(self, **kwargs):
        return self._builder.build_task_confirmation_card(**kwargs)

    def build_clarification_card(self, **kwargs):
        return self._builder.build_clarification_card(**kwargs)
