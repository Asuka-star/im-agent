from __future__ import annotations

import json
import logging
import re

from app.services.presentation_artifact_service import PresentationArtifactService


logger = logging.getLogger(__name__)


class PresentationTool:
    """Handles presentation package formatting, artifact persistence, and local revisions."""

    def __init__(self, *, artifact_service: PresentationArtifactService) -> None:
        self.artifact_service = artifact_service

    def persist_artifact(
        self,
        package: dict,
        *,
        provider: str,
        session_id: str,
        task_run_id: str | None,
    ) -> dict:
        try:
            return self.artifact_service.persist_package(
                package,
                provider=provider,
                task_run_id=task_run_id,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist presentation artifact, keeping preview only: %s", exc)
            return {
                "artifact_type": "slides_package",
                "provider": provider,
                "status": "ready",
                "title": str(package.get("theme") or "演示稿"),
                "preview": package,
            }

    def format_reply(self, package: dict) -> str:
        theme = str(package.get("theme") or "基于群聊讨论的协作汇报").strip()
        audience = str(package.get("audience") or "项目汇报 / 路演准备").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        lines = ["【汇报大纲】", f"主题：{theme}", f"适用场景：{audience}"]
        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"第{index}页").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            lines.append(f"P{index}. {title}")
            for bullet in bullets[:4]:
                lines.append(f"- {str(bullet).strip()}")

        if emphasis:
            lines.append("演示时重点强调：")
            for item in emphasis[:3]:
                lines.append(f"- {str(item).strip()}")

        if assets:
            lines.append("建议补充素材：")
            for item in assets[:4]:
                lines.append(f"- {str(item).strip()}")

        return "\n".join(lines)

    def resolve_slides_artifact(self, artifacts: list | None, *, artifact_id: str | None = None):
        requested_id = (artifact_id or "").strip()
        for artifact in reversed(artifacts or []):
            artifact_type = self.artifact_field(artifact, "artifact_type")
            current_artifact_id = self.artifact_field(artifact, "artifact_id")
            if artifact_type != "slides_package":
                continue
            if requested_id and current_artifact_id != requested_id:
                continue
            return artifact
        return None

    def artifact_field(self, artifact: object, field: str):
        if isinstance(artifact, dict):
            return artifact.get(field)
        return getattr(artifact, field, None)

    def preview_payload(self, preview_json: str | None) -> dict:
        if not preview_json:
            return {}
        try:
            payload = json.loads(preview_json)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def revise_deterministic(self, package: dict, instruction: str) -> dict:
        revised = json.loads(json.dumps(package, ensure_ascii=False))
        revised["revision_instruction"] = instruction
        slides = revised.get("slides") if isinstance(revised.get("slides"), list) else []
        target_index = self._target_slide_index(instruction, len(slides))
        if target_index is not None and 0 <= target_index < len(slides) and isinstance(slides[target_index], dict):
            slide = slides[target_index]
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            note = str(slide.get("speaker_notes") or slide.get("notes") or "").strip()
            slide["speaker_notes"] = f"{note}\n修订要求：{instruction}".strip()
            if instruction not in {str(item).strip() for item in bullets}:
                slide["bullets"] = [*bullets[:4], f"修订要求：{instruction}"]
        elif "新增" in instruction or "加一页" in instruction or "加一张" in instruction:
            slides.append(
                {
                    "title": "补充说明",
                    "bullets": [instruction],
                    "speaker_notes": f"本页用于补充说明：{instruction}",
                    "duration_sec": 45,
                }
            )
        elif slides and isinstance(slides[-1], dict):
            emphasis = revised.get("emphasis") if isinstance(revised.get("emphasis"), list) else []
            revised["emphasis"] = [*emphasis, instruction]

        if any(token in instruction for token in ("压缩到5页", "压缩成5页", "缩成5页", "五页", "5 页")) and len(slides) > 5:
            del slides[5:]
        revised["slides"] = slides
        return revised

    def _target_slide_index(self, instruction: str, total: int) -> int | None:
        match = re.search(r"(?:第|P)\s*(\d+)\s*(?:页|张|p)?", instruction, re.IGNORECASE)
        if not match:
            return None
        index = int(match.group(1)) - 1
        if index < 0 or index >= total:
            return None
        return index
