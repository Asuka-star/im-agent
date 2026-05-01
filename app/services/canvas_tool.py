from __future__ import annotations

from app.services.canvas_artifact_service import CanvasArtifactService


class CanvasTool:
    """Generates free-canvas artifacts and concise user-facing previews."""

    def __init__(self, *, artifact_service: CanvasArtifactService) -> None:
        self.artifact_service = artifact_service

    def generate_flow_artifact(
        self,
        *,
        title: str,
        instruction: str,
        llm_result: dict,
        workspace_context: str,
        task_run_id: str | None,
        session_id: str,
    ) -> dict:
        return self.artifact_service.generate_flow(
            title=title,
            instruction=instruction,
            llm_result=llm_result,
            workspace_context=workspace_context,
            task_run_id=task_run_id,
            session_id=session_id,
        )

    def format_reply(self, artifact: dict) -> str:
        preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
        shapes = preview.get("shapes") if isinstance(preview.get("shapes"), list) else []
        return (
            "【Canvas 产物】\n"
            f"标题：{artifact.get('title') or 'Canvas'}\n"
            f"节点/连线数量：{len(shapes)}\n"
            f"预览链接：{artifact.get('url') or ''}"
        )
