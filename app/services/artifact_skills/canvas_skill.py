from __future__ import annotations

from typing import Any

from app.services.artifact_skills.base import ArtifactSkill, SkillCheckResult
from app.services.artifact_skills.style_tokens import css_color


class CanvasSkill(ArtifactSkill):
    artifact_type = "canvas"
    node_palette = [
        {"color": css_color("info_soft"), "stroke": "#5A9FD6", "group": "Input"},
        {"color": "#F1F3FF", "stroke": "#7B71D8", "group": "Agent"},
        {"color": css_color("success_soft"), "stroke": "#67A77B", "group": "Tools"},
        {"color": css_color("accent_soft"), "stroke": css_color("accent"), "group": "Artifact"},
    ]

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        scene = dict(payload or {})
        scene.setdefault("schema", "im-agent.canvas.v1")
        scene.setdefault("version", 1)
        return scene

    def verify(self, payload: dict[str, Any]) -> SkillCheckResult:
        shapes = payload.get("shapes") if isinstance(payload.get("shapes"), list) else []
        warnings = [] if shapes else ["canvas_empty"]
        return SkillCheckResult(ok=not warnings, warnings=warnings)
