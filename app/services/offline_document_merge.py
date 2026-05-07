from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from app.services.document_section_utils import (
    normalize_section_paragraphs,
    paragraph_signature,
    paragraph_preview_text,
    section_snapshot_map,
)


class OfflineDocumentMergeService:
    """Build a structured merge plan for offline document confirmation."""

    def build_merge_plan(
        self,
        package: dict,
        *,
        current_document: dict | None = None,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        normalized_sections = [
            {
                "heading": str(section.get("heading") or "").strip(),
                "paragraphs": normalize_section_paragraphs(
                    section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else [],
                ),
            }
            for section in sections
            if isinstance(section, dict)
        ]
        desired_map = section_snapshot_map(normalized_sections)
        current_snapshot = (
            current_document.get("section_snapshot")
            if isinstance(current_document, dict) and isinstance(current_document.get("section_snapshot"), list)
            else []
        )
        current_map = section_snapshot_map(current_snapshot)

        updated_headings: list[str] = []
        new_headings: list[str] = []
        unchanged_headings: list[str] = []
        section_operations: list[dict[str, Any]] = []
        preview_lines: list[str] = []
        text_paragraph_count = 0
        image_count = 0
        table_count = 0
        callout_count = 0

        for section in normalized_sections:
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            current_paragraphs = current_map.get(heading) or []
            if not heading:
                continue
            if heading not in current_map:
                change_type = "append"
                new_headings.append(heading)
            elif current_paragraphs != paragraphs:
                change_type = "update"
                updated_headings.append(heading)
            else:
                change_type = "unchanged"
                unchanged_headings.append(heading)

            section_text_count, section_image_count, section_table_count, section_callout_count, section_previews = self._section_stats(paragraphs)
            _, current_image_count, current_table_count, current_callout_count, current_previews = self._section_stats(current_paragraphs)
            text_paragraph_count += section_text_count
            image_count += section_image_count
            table_count += section_table_count
            callout_count += section_callout_count
            for preview in section_previews:
                if preview not in preview_lines:
                    preview_lines.append(preview)
                if len(preview_lines) >= 4:
                    break

            overlap_count = len(
                set(paragraph_signature(item) for item in current_paragraphs)
                & set(paragraph_signature(item) for item in paragraphs)
            )
            similarity = self._section_similarity(current_paragraphs, paragraphs)
            paragraph_delta_count = abs(len(paragraphs) - len(current_paragraphs))
            risk_flags: list[str] = []
            if change_type == "update" and similarity < 0.35:
                risk_flags.append("rewrite_heavy")
            if (
                change_type == "update"
                and (current_image_count or current_table_count or section_image_count or section_table_count)
            ):
                risk_flags.append("rich_media_update")
            if change_type == "append" and (section_image_count or section_table_count):
                risk_flags.append("rich_media_append")
            if change_type == "update" and paragraph_delta_count >= 3:
                risk_flags.append("paragraph_count_jump")
            if change_type == "update" and not overlap_count and current_paragraphs and paragraphs:
                risk_flags.append("no_paragraph_overlap")

            if change_type == "unchanged":
                risk_level = "low"
            elif "rewrite_heavy" in risk_flags or "no_paragraph_overlap" in risk_flags:
                risk_level = "high"
            elif risk_flags:
                risk_level = "medium"
            else:
                risk_level = "low"
            requires_review = change_type == "update" and risk_level in {"medium", "high"}

            section_operations.append(
                {
                    "heading": heading,
                    "change_type": change_type,
                    "current_paragraph_count": len(current_paragraphs),
                    "next_paragraph_count": len(paragraphs),
                    "text_paragraph_count": section_text_count,
                    "image_count": section_image_count,
                    "table_count": section_table_count,
                    "callout_count": section_callout_count,
                    "current_image_count": current_image_count,
                    "current_table_count": current_table_count,
                    "current_callout_count": current_callout_count,
                    "paragraph_overlap_count": overlap_count,
                    "paragraph_delta_count": paragraph_delta_count,
                    "similarity": similarity,
                    "risk_level": risk_level,
                    "risk_flags": risk_flags,
                    "requires_review": requires_review,
                    "preview_lines": section_previews[:3],
                    "current_preview_lines": current_previews[:3],
                    "next_preview_lines": section_previews[:3],
                }
            )

        current_document_missing = not (
            isinstance(current_document, dict)
            and str(current_document.get("document_id") or "").strip()
        )
        missing_current_headings = [
            heading
            for heading in current_map
            if heading and heading not in desired_map
        ]

        warning_flags: list[str] = []
        if current_document_missing:
            warning_flags.append("current_document_missing")
        if not updated_headings and not new_headings:
            warning_flags.append("no_structural_changes_detected")
        if desired_map and missing_current_headings and len(desired_map) < len(current_map):
            warning_flags.append("partial_offline_document")
        if image_count or table_count:
            warning_flags.append("contains_rich_media")

        confidence = 0.55
        if not current_document_missing:
            confidence += 0.20
        if updated_headings or new_headings:
            confidence += 0.15
        if image_count or table_count:
            confidence += 0.05
        if "current_document_missing" in warning_flags:
            confidence -= 0.15
        if "no_structural_changes_detected" in warning_flags:
            confidence -= 0.20
        if "partial_offline_document" in warning_flags:
            confidence -= 0.10
        confidence = max(0.05, min(round(confidence, 2), 0.98))

        conflicts: list[dict[str, Any]] = []
        if current_document_missing:
            conflicts.append(
                {
                    "type": "missing_current_document",
                    "severity": "medium",
                    "message": "Current requirement does not have a pinned collaborative document yet.",
                }
            )
        if "partial_offline_document" in warning_flags:
            conflicts.append(
                {
                    "type": "partial_document_context",
                    "severity": "medium",
                    "message": "Offline sections cover only part of the current document snapshot.",
                    "headings": missing_current_headings[:8],
                }
            )
        if "no_structural_changes_detected" in warning_flags:
            conflicts.append(
                {
                    "type": "no_structural_changes_detected",
                    "severity": "low",
                    "message": "No clear section-level delta was detected from the offline document.",
                }
            )
        for operation in section_operations:
            if operation.get("change_type") != "update":
                continue
            if operation.get("risk_level") == "high":
                conflicts.append(
                    {
                        "type": "section_rewrite_high_delta",
                        "severity": "high",
                        "heading": operation.get("heading"),
                        "message": "The offline document appears to heavily rewrite an existing section.",
                        "risk_flags": list(operation.get("risk_flags") or []),
                        "current_preview_lines": list(operation.get("current_preview_lines") or []),
                        "next_preview_lines": list(operation.get("next_preview_lines") or []),
                    }
                )
            elif "rich_media_update" in list(operation.get("risk_flags") or []):
                conflicts.append(
                    {
                        "type": "section_rich_media_update",
                        "severity": "medium",
                        "heading": operation.get("heading"),
                        "message": "This section updates rich-media blocks and may need a quick manual check.",
                        "risk_flags": list(operation.get("risk_flags") or []),
                    }
                )

        auto_merge_eligible = (
            not current_document_missing
            and confidence >= 0.75
            and "no_structural_changes_detected" not in warning_flags
            and not any(
                str(item.get("severity") or "").strip().lower() in {"high", "medium"}
                for item in conflicts
            )
        )
        summary_lines = [
            f"拟更新章节 {len(updated_headings)} 个",
            f"新增章节 {len(new_headings)} 个",
            f"图片 {image_count} 张 / 表格 {table_count} 个",
            f"合并置信度 {confidence:.2f}",
        ]
        if callout_count:
            summary_lines.append(f"提示块 {callout_count} 个")
        if updated_headings:
            summary_lines.append(f"更新章节：{'、'.join(updated_headings[:4])}")
        if new_headings:
            summary_lines.append(f"新增章节：{'、'.join(new_headings[:4])}")
        if preview_lines:
            summary_lines.append(f"内容预览：{'；'.join(preview_lines[:3])}")

        base_version = current_document.get("version") if isinstance(current_document, dict) else None
        return {
            "merge_mode": "safe_section_merge",
            "recommended_action": "auto_merge" if auto_merge_eligible else "confirm_merge",
            "auto_merge_eligible": auto_merge_eligible,
            "confidence": confidence,
            "instruction": str(instruction or "").strip() or None,
            "base_document_id": str(current_document.get("document_id") or "").strip() if isinstance(current_document, dict) else None,
            "base_document_title": str(current_document.get("title") or "").strip() if isinstance(current_document, dict) else None,
            "base_document_version": base_version if isinstance(base_version, int) else None,
            "section_count": len([section for section in normalized_sections if section.get("heading") or section.get("paragraphs")]),
            "text_paragraph_count": text_paragraph_count,
            "image_count": image_count,
            "table_count": table_count,
            "callout_count": callout_count,
            "updated_headings": updated_headings,
            "new_headings": new_headings,
            "unchanged_headings": unchanged_headings[:8],
            "missing_current_headings": missing_current_headings[:8],
            "changed_section_count": len(updated_headings) + len(new_headings),
            "conflict_count": len(conflicts),
            "section_operations": section_operations,
            "conflicts": conflicts,
            "preview_lines": preview_lines[:4],
            "warning_flags": warning_flags,
            "summary_lines": summary_lines,
        }

    def summarize_for_confirmation(self, package: dict, *, current_document: dict | None = None) -> dict[str, Any]:
        return self.build_merge_plan(package, current_document=current_document)

    @staticmethod
    def _section_stats(paragraphs: list[Any]) -> tuple[int, int, int, int, list[str]]:
        text_count = 0
        image_count = 0
        table_count = 0
        callout_count = 0
        previews: list[str] = []
        for paragraph in paragraphs:
            if isinstance(paragraph, str):
                text_count += 1
            elif isinstance(paragraph, dict):
                paragraph_type = str(paragraph.get("type") or "").strip().lower()
                if paragraph_type == "image":
                    image_count += 1
                elif paragraph_type == "table":
                    table_count += 1
                elif paragraph_type in {"callout", "note"}:
                    callout_count += 1
                else:
                    text_count += 1
            preview = paragraph_preview_text(paragraph)
            if preview and preview not in previews:
                previews.append(preview)
            if len(previews) >= 3:
                break
        return text_count, image_count, table_count, callout_count, previews

    @staticmethod
    def _section_similarity(current_paragraphs: list[Any], next_paragraphs: list[Any]) -> float:
        current_text = "\n".join(
            preview for preview in (paragraph_preview_text(item) for item in current_paragraphs) if preview
        )
        next_text = "\n".join(
            preview for preview in (paragraph_preview_text(item) for item in next_paragraphs) if preview
        )
        if not current_text and not next_text:
            return 1.0
        if not current_text or not next_text:
            return 0.0
        return round(SequenceMatcher(None, current_text, next_text).ratio(), 2)
