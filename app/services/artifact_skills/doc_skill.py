from __future__ import annotations

import re
from typing import Any

from app.services.artifact_skills.base import ArtifactSkill, SkillCheckResult
from app.services.tools.doc_tool import DocTool


class DocSkill(ArtifactSkill):
    artifact_type = "doc"
    _PRESENTATION_HEADING = re.compile(r"^\s*(?:P|Slide)\s*\d+[\.\):：、-]\s+", re.IGNORECASE)
    _PRESENTATION_ONLY_HEADINGS = {
        "presentation outline",
        "speaker notes",
        "suggested assets",
        "slide notes",
        "\u6587\u6863\u8bf4\u660e",
        "\u6f14\u793a\u91cd\u70b9",
        "\u5efa\u8bae\u8865\u5145\u7d20\u6750",
    }

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        package = dict(payload or {})
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        normalized = DocTool.normalize_doc_sections(sections)
        package["sections"] = [section for section in normalized if not self.is_presentation_outline_section(section)]
        return package

    def verify(self, payload: dict[str, Any]) -> SkillCheckResult:
        sections = payload.get("sections") if isinstance(payload.get("sections"), list) else []
        warnings = [
            f"presentation_outline_section:{section.get('heading')}"
            for section in sections
            if isinstance(section, dict) and self.is_presentation_outline_section(section)
        ]
        return SkillCheckResult(ok=not warnings, warnings=warnings)

    @classmethod
    def is_presentation_outline_section(cls, section: dict[str, Any]) -> bool:
        heading = str(section.get("heading") or "").strip()
        if cls._PRESENTATION_HEADING.search(heading):
            return True
        return heading.lower() in cls._PRESENTATION_ONLY_HEADINGS
