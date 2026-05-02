from __future__ import annotations

import json
import logging
import re

from app.core.config import settings
from app.services.artifact_edit_plan import ArtifactEditPlan, ArtifactEditPlanner
from app.services.presentation_artifact_service import PresentationArtifactService


logger = logging.getLogger(__name__)


class PresentationTool:
    """Handles presentation package formatting, artifact persistence, and local revisions."""

    PUBLIC_PREVIEW_BASE_URL = settings.artifact_public_base_url

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

    def format_reply(self, package: dict, *, artifact: dict | None = None) -> str:
        if artifact and isinstance(artifact.get("preview"), dict):
            package = artifact["preview"]
        theme = str(package.get("theme") or "基于群聊讨论的协作汇报").strip()
        audience = str(package.get("audience") or "项目汇报 / 路演准备").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []
        exports = package.get("exports") if isinstance(package.get("exports"), dict) else {}

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

        preview_url = self.public_preview_url(artifact.get("url") if artifact else exports.get("html"))
        pptx_url = self.public_preview_url(exports.get("pptx"))
        pdf_url = self.public_preview_url(exports.get("pdf"))
        if preview_url or pptx_url or pdf_url:
            lines.append("产物链接：")
            if preview_url:
                lines.append(f"- 预览链接：{preview_url}")
            if pptx_url:
                lines.append(f"- PPT 下载：{pptx_url}")
            if pdf_url:
                lines.append(f"- PDF 下载：{pdf_url}")

        return "\n".join(lines)

    @classmethod
    def public_preview_url(cls, value: object) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        if not url.startswith("/"):
            url = f"/{url}"
        return f"{cls.PUBLIC_PREVIEW_BASE_URL.rstrip('/')}{url}"

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

    def plan_revision(self, package: dict, instruction: str, llm_result: dict | None = None) -> ArtifactEditPlan:
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        targets: list[str] = []
        for index, slide in enumerate(slides, start=1):
            title = str(slide.get("title") or "").strip() if isinstance(slide, dict) else ""
            targets.extend([f"P{index}", f"第{index}页", f"第{index}张"])
            if title:
                targets.extend([title, f"P{index} {title}", f"第{index}页 {title}"])
        return ArtifactEditPlanner.from_llm_result(
            llm_result,
            artifact_type="slides",
            instruction=instruction,
            available_targets=targets,
        )

    def revise_deterministic(
        self,
        package: dict,
        instruction: str,
        *,
        edit_plan: ArtifactEditPlan | None = None,
    ) -> dict:
        edit_plan = edit_plan or self.plan_revision(package, instruction)
        revised = json.loads(json.dumps(package, ensure_ascii=False))
        revised["revision_instruction"] = instruction
        revised["artifact_edit_plan"] = self.serialize_edit_plan(edit_plan)
        slides = revised.get("slides") if isinstance(revised.get("slides"), list) else []

        for operation in edit_plan.operations or []:
            target_indices = self._operation_target_indices(operation.target, instruction, slides)
            if operation.op_type == "delete":
                for index in sorted(target_indices, reverse=True):
                    if 0 <= index < len(slides):
                        del slides[index]
                continue
            if operation.op_type == "append":
                text = str(operation.payload.get("text") or instruction).strip() or instruction
                slides.append(
                    {
                        "title": "补充说明",
                        "bullets": [text],
                        "speaker_notes": f"本页用于补充说明：{text}",
                        "duration_sec": 45,
                    }
                )
                continue
            if operation.op_type == "compress":
                max_count = operation.payload.get("max_count") or ArtifactEditPlanner.extract_requested_count(instruction)
                if max_count and len(slides) > int(max_count):
                    del slides[int(max_count) :]
                continue
            if operation.op_type in {"rewrite", "update", "rename", "reorder"}:
                if not target_indices and self._target_is_specific(operation.target):
                    continue
                self._mark_slides_revised(slides, target_indices, instruction)

        if not edit_plan.operations:
            target_index = self._target_slide_index(instruction, len(slides))
            if target_index is not None:
                self._mark_slides_revised(slides, [target_index], instruction)
            elif slides and isinstance(slides[-1], dict):
                emphasis = revised.get("emphasis") if isinstance(revised.get("emphasis"), list) else []
                revised["emphasis"] = [*emphasis, instruction]

        max_count = ArtifactEditPlanner.extract_requested_count(instruction)
        if max_count and len(slides) > max_count and "compress" in edit_plan.operation_types:
            del slides[max_count:]
        revised["slides"] = slides
        return revised

    @staticmethod
    def serialize_edit_plan(edit_plan: ArtifactEditPlan) -> dict:
        return {
            "artifact_type": edit_plan.artifact_type,
            "mutation_required": edit_plan.mutation_required,
            "scope": edit_plan.scope,
            "fallback": edit_plan.fallback,
            "needs_clarification": edit_plan.needs_clarification,
            "question": edit_plan.question,
            "operations": [
                {
                    "type": operation.op_type,
                    "target": operation.target,
                    "payload": operation.payload,
                    "reason": operation.reason,
                }
                for operation in edit_plan.operations
            ],
        }

    @staticmethod
    def package_changed(before: dict, after: dict) -> bool:
        ignored = {"version", "revision_instruction", "artifact_edit_plan"}

        def comparable(payload: dict) -> dict:
            return {key: value for key, value in payload.items() if key not in ignored}

        return comparable(before) != comparable(after)

    def _operation_target_indices(self, target: dict, instruction: str, slides: list) -> list[int]:
        label_to_index: dict[str, int] = {}
        for index, slide in enumerate(slides):
            title = str(slide.get("title") or "").strip() if isinstance(slide, dict) else ""
            labels = [f"P{index + 1}", f"第{index + 1}页", f"第{index + 1}张"]
            if title:
                labels.extend([title, f"P{index + 1} {title}", f"第{index + 1}页 {title}"])
            for label in labels:
                key = ArtifactEditPlanner.match_key(label)
                if key:
                    label_to_index[key] = index

        if isinstance(target, dict) and target.get("scope") == "all":
            return list(range(len(slides)))

        matched_labels = ArtifactEditPlanner.resolve_target_payload_mentions(
            target if isinstance(target, dict) else {},
            list(label_to_index.keys()),
            allow_all=False,
        )
        if matched_labels:
            return sorted({label_to_index[ArtifactEditPlanner.match_key(label)] for label in matched_labels})

        page_index = self._target_slide_index(instruction, len(slides))
        return [page_index] if page_index is not None else []

    def _mark_slides_revised(self, slides: list, target_indices: list[int], instruction: str) -> None:
        indices = target_indices or list(range(len(slides)))
        for index in indices:
            if not (0 <= index < len(slides)) or not isinstance(slides[index], dict):
                continue
            slide = slides[index]
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            note = str(slide.get("speaker_notes") or slide.get("notes") or "").strip()
            slide["speaker_notes"] = f"{note}\n修订要求：{instruction}".strip()
            if instruction not in {str(item).strip() for item in bullets}:
                slide["bullets"] = [*bullets[:4], f"修订要求：{instruction}"]

    @staticmethod
    def _target_is_specific(target: dict) -> bool:
        if not isinstance(target, dict) or target.get("scope") == "all":
            return False
        if isinstance(target.get("queries"), list) and target.get("queries"):
            return True
        return any(str(target.get(key) or "").strip() for key in ("query", "heading", "title", "name", "id", "label"))

    def _target_slide_index(self, instruction: str, total: int) -> int | None:
        match = re.search(r"(?:第|P)\s*(\d+)\s*(?:页|张|p)?", instruction, re.IGNORECASE)
        if not match:
            return None
        index = int(match.group(1)) - 1
        if index < 0 or index >= total:
            return None
        return index
