from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from app.services.artifact_skills.base import SkillCheckResult
from app.services.artifact_skills.canvas_skill import CanvasSkill
from app.services.artifact_skills.doc_skill import DocSkill
from app.services.artifact_skills.slides_skill import SlidesSkill


class ArtifactVerifier:
    def __init__(self) -> None:
        self.doc_skill = DocSkill()
        self.slides_skill = SlidesSkill()
        self.canvas_skill = CanvasSkill()

    def verify_doc(self, package: dict[str, Any]) -> SkillCheckResult:
        return self.doc_skill.verify(package)

    def verify_slides(self, package: dict[str, Any]) -> SkillCheckResult:
        return self.slides_skill.verify(package)

    def verify_canvas(self, scene: dict[str, Any]) -> SkillCheckResult:
        return self.canvas_skill.verify(scene)

    def verify_pptx_file(self, path: Path) -> SkillCheckResult:
        warnings: list[str] = []
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            return SkillCheckResult(ok=False, warnings=[f"pptx_zip_invalid:{exc}"])

        required = {"[Content_Types].xml", "ppt/presentation.xml"}
        missing = sorted(required - names)
        warnings.extend(f"pptx_missing:{name}" for name in missing)
        if not any(name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in names):
            warnings.append("pptx_missing:slides")
        return SkillCheckResult(ok=not warnings, warnings=warnings)
