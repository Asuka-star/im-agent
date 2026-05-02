from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillCheckResult:
    ok: bool
    warnings: list[str] = field(default_factory=list)


class ArtifactSkill:
    """Small contract for artifact-specific normalization and validation."""

    artifact_type = "artifact"

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload or {})

    def verify(self, payload: dict[str, Any]) -> SkillCheckResult:
        return SkillCheckResult(ok=True)
