from __future__ import annotations

from typing import Any

from app.services.artifact_skills.base import ArtifactSkill, SkillCheckResult
from app.services.artifact_skills.style_tokens import ARTIFACT_STYLE
from app.utils.values import coerce_positive_int


class SlidesSkill(ArtifactSkill):
    artifact_type = "slides"

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        package = dict(payload or {})
        package.setdefault("schema", "im-agent.slides.v1")
        package["version"] = coerce_positive_int(package.get("version"))
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        package["slides"] = [self.normalize_slide(slide, index) for index, slide in enumerate(slides, start=1)]
        package["style"] = dict(ARTIFACT_STYLE)
        return package

    def normalize_slide(self, slide: object, index: int) -> dict[str, Any]:
        payload = slide if isinstance(slide, dict) else {}
        title = str(payload.get("title") or f"Slide {index}").strip() or f"Slide {index}"
        bullets = payload.get("bullets") if isinstance(payload.get("bullets"), list) else []
        notes = (
            payload.get("speaker_notes")
            or payload.get("speaker_note")
            or payload.get("notes")
            or self.fallback_speaker_notes(title, bullets)
        )
        duration = payload.get("duration_sec") or payload.get("duration_seconds") or 45
        try:
            duration_sec = max(15, min(int(duration), 180))
        except (TypeError, ValueError):
            duration_sec = 45
        return {
            **payload,
            "title": title,
            "bullets": [str(item).strip() for item in bullets if str(item).strip()][:5],
            "speaker_notes": str(notes).strip(),
            "duration_sec": duration_sec,
        }

    def fallback_speaker_notes(self, title: str, bullets: list[object]) -> str:
        key_points = "; ".join(str(item).strip() for item in bullets[:3] if str(item).strip())
        if key_points:
            return f"Talk through {title}: {key_points}."
        return f"Use this slide to introduce {title} and close with the main takeaway."

    def verify(self, payload: dict[str, Any]) -> SkillCheckResult:
        slides = payload.get("slides") if isinstance(payload.get("slides"), list) else []
        warnings: list[str] = []
        if not slides:
            warnings.append("slides_empty")
        for index, slide in enumerate(slides, start=1):
            if not isinstance(slide, dict):
                warnings.append(f"slide_{index}_invalid")
                continue
            if not str(slide.get("title") or "").strip():
                warnings.append(f"slide_{index}_missing_title")
            if len(slide.get("bullets") if isinstance(slide.get("bullets"), list) else []) > 5:
                warnings.append(f"slide_{index}_too_many_bullets")
        return SkillCheckResult(ok=not warnings, warnings=warnings)
