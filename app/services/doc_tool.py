from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.feishu.doc_api import FeishuDocAPI
from app.services.session_document_service import SessionDocumentService
from app.utils.values import coerce_positive_int


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentSyncResult:
    mode: str
    status: str
    summary_lines: list[str]
    document_info: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def url(self) -> str | None:
        return str(self.document_info.get("url") or "").strip() or None

    @property
    def version(self) -> int:
        return coerce_positive_int(self.document_info.get("version"))


class DocTool:
    """First-class document sync tool used by the workflow planner/executor."""

    def __init__(
        self,
        *,
        doc_api: FeishuDocAPI,
        session_document_service: SessionDocumentService,
    ) -> None:
        self.doc_api = doc_api
        self.session_document_service = session_document_service

    def sync_package_to_session_doc(
        self,
        package: dict,
        *,
        session_id: str,
        episode_id: int | None,
        instruction: str,
        task_run_id: str | None = None,
        target_document: dict | None = None,
    ) -> DocumentSyncResult:
        title = str(package.get("title") or "collab_doc").strip() or "collab_doc"
        sections = self.normalize_doc_sections(
            package.get("sections") if isinstance(package.get("sections"), list) else [],
        )
        package["sections"] = sections

        if not self.doc_api.is_configured():
            logger.warning("Feishu doc sync skipped because FEISHU_DOC_ENABLED is not enabled.")
            lines = ["- Feishu doc sync is disabled. Set FEISHU_DOC_ENABLED=true first."]
            return DocumentSyncResult(mode="local_only", status="local_ready", summary_lines=lines, error=lines[0])

        current_doc = target_document if isinstance(target_document, dict) else self.session_document_service.get_current_document(session_id)
        current_snapshot = current_doc.get("section_snapshot") if isinstance(current_doc, dict) else None
        if current_doc and current_doc.get("document_id"):
            try:
                change_plan = self.plan_doc_section_changes(
                    sections,
                    instruction=instruction,
                    previous_snapshot=current_snapshot if isinstance(current_snapshot, list) else None,
                )
                if (
                    not change_plan["changed_headings"]
                    and not change_plan["deleted_headings"]
                    and not change_plan["rename_map"]
                ):
                    remembered = self.session_document_service.save_current_document(
                        session_id,
                        document_id=str(current_doc["document_id"]),
                        url=str(current_doc.get("url") or "").strip() or None,
                        title=str(current_doc.get("title") or title),
                        episode_id=episode_id,
                        task_run_id=task_run_id,
                        version=coerce_positive_int(current_doc.get("version")),
                        sync_mode="noop",
                        section_snapshot=self.session_document_service.build_section_snapshot(
                            current_snapshot if isinstance(current_snapshot, list) else sections,
                        ),
                        section_block_index=current_doc.get("section_block_index")
                        if isinstance(current_doc.get("section_block_index"), list)
                        else [],
                    )
                    lines = self.build_doc_sync_lines(
                        "noop",
                        remembered,
                        changed_headings=change_plan["targeted_headings"],
                    )
                    return DocumentSyncResult(
                        mode="noop",
                        status="ready",
                        summary_lines=lines,
                        document_info=remembered,
                    )

                replaced = self.doc_api.replace_document_sections(
                    str(current_doc["document_id"]),
                    str(current_doc.get("title") or title),
                    change_plan["desired_sections"],
                    target_headings=change_plan["changed_headings"],
                    delete_headings=change_plan["deleted_headings"],
                    rename_map=change_plan["rename_map"],
                )
                version = coerce_positive_int(current_doc.get("version")) + 1
                merged_snapshot = self.merge_doc_section_snapshots(
                    current_snapshot if isinstance(current_snapshot, list) else [],
                    change_plan["desired_sections"],
                    deleted_headings=change_plan["deleted_headings"],
                    rename_map=change_plan["rename_map"],
                )
                remembered = self.session_document_service.save_current_document(
                    session_id,
                    document_id=str(replaced.get("document_id") or current_doc["document_id"]),
                    url=str(replaced.get("url") or current_doc.get("url") or "").strip() or None,
                    title=str(current_doc.get("title") or title),
                    episode_id=episode_id,
                    task_run_id=task_run_id,
                    version=version,
                    sync_mode="updated",
                    section_snapshot=merged_snapshot,
                    section_block_index=replaced.get("section_block_index")
                    if isinstance(replaced.get("section_block_index"), list)
                    else [],
                )
                lines = self.build_doc_sync_lines(
                    "updated",
                    remembered,
                    replaced_block_count=int(replaced.get("replaced_block_count") or 0),
                    inserted_block_count=int(replaced.get("inserted_block_count") or 0),
                    changed_headings=change_plan["changed_headings"],
                    appended_headings=replaced.get("appended_headings")
                    if isinstance(replaced.get("appended_headings"), list)
                    else [],
                    deleted_headings=replaced.get("deleted_headings")
                    if isinstance(replaced.get("deleted_headings"), list)
                    else [],
                    renamed_headings=replaced.get("renamed_headings")
                    if isinstance(replaced.get("renamed_headings"), list)
                    else [],
                    patched_headings=replaced.get("patched_headings")
                    if isinstance(replaced.get("patched_headings"), list)
                    else [],
                )
                return DocumentSyncResult(
                    mode="updated",
                    status="ready",
                    summary_lines=lines,
                    document_info=remembered,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Feishu doc section replacement failed, recreating document: %s", exc)

        try:
            created = self.doc_api.create_document_from_sections(title, sections)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Feishu doc sync failed: %s", exc)
            lines = [f"- Document sync failed: {exc}"]
            return DocumentSyncResult(mode="sync_failed", status="sync_failed", summary_lines=lines, error=str(exc))

        remembered = self.session_document_service.save_current_document(
            session_id,
            document_id=str(created["document_id"]),
            url=str(created.get("url") or "").strip() or None,
            title=str(created.get("title") or title),
            episode_id=episode_id,
            task_run_id=task_run_id,
            version=1,
            sync_mode="created",
            section_snapshot=self.session_document_service.build_section_snapshot(sections),
            section_block_index=created.get("section_block_index")
            if isinstance(created.get("section_block_index"), list)
            else [],
        )
        lines = self.build_doc_sync_lines(
            "created",
            remembered,
            folder_scope=str(created.get("folder_scope") or "").strip(),
            folder_url=str(created.get("folder_url") or "").strip() or None,
            folder_note=str(created.get("folder_note") or "").strip() or None,
        )
        return DocumentSyncResult(
            mode="created",
            status="ready",
            summary_lines=lines,
            document_info=remembered,
        )

    @staticmethod
    def normalize_doc_sections(sections: list[dict]) -> list[dict]:
        canonical_aliases = {
            "摘要": "讨论摘要",
            "总结": "讨论摘要",
            "概述": "讨论摘要",
            "背景": "讨论摘要",
            "待办": "任务清单",
            "任务": "任务清单",
            "行动项": "下一步建议",
            "后续动作": "下一步建议",
            "风险": "风险与卡点",
            "卡点": "风险与卡点",
        }
        preferred_order = [
            "文档说明",
            "讨论摘要",
            "任务清单",
            "风险与卡点",
            "下一步建议",
            "演示重点",
            "建议补充素材",
        ]
        grouped: dict[str, list[str]] = {}
        appearance_order: list[str] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            raw_heading = str(section.get("heading") or "").strip()
            canonical_heading = canonical_aliases.get(raw_heading, raw_heading or "未命名章节")
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned: list[str] = []
            for item in paragraphs:
                text = str(item).strip()
                if text and text not in cleaned:
                    cleaned.append(text)
            if not raw_heading and not cleaned:
                continue
            if canonical_heading not in grouped:
                grouped[canonical_heading] = []
                appearance_order.append(canonical_heading)
            for text in cleaned:
                if text not in grouped[canonical_heading]:
                    grouped[canonical_heading].append(text)

        ordered_headings = [heading for heading in preferred_order if heading in grouped]
        ordered_headings.extend(heading for heading in appearance_order if heading not in ordered_headings)
        return [
            {"heading": heading, "paragraphs": grouped[heading]}
            for heading in ordered_headings
            if grouped.get(heading)
        ]

    def build_incremental_doc_sections(
        self,
        package: dict,
        *,
        instruction: str,
        previous_snapshot: list[dict] | None = None,
    ) -> tuple[list[dict], list[str], list[str]]:
        timestamp = str(package.get("stats_as_of") or self._doc_title_timestamp()).strip()
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        targeted_headings = self.resolve_doc_update_targets(instruction, sections)
        incremental_sections: list[dict] = [
            {
                "heading": f"Update ({timestamp})",
                "paragraphs": self.build_doc_update_header_lines(
                    instruction=instruction,
                    targeted_headings=targeted_headings,
                ),
            }
        ]
        previous_map = self.section_snapshot_map(previous_snapshot or [])
        changed_headings: list[str] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "supplement").strip() or "supplement"
            if targeted_headings and heading not in targeted_headings:
                continue
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
            if not cleaned:
                continue
            if previous_map.get(heading) == cleaned:
                continue
            changed_headings.append(heading)
            incremental_sections.append({"heading": f"refresh: {heading}", "paragraphs": cleaned})
        return incremental_sections, changed_headings, targeted_headings

    @classmethod
    def plan_doc_section_changes(
        cls,
        sections: list[dict],
        *,
        instruction: str,
        previous_snapshot: list[dict] | None = None,
    ) -> dict[str, object]:
        previous_snapshot = previous_snapshot or []
        desired_sections = cls.normalize_doc_sections(sections)
        previous_map = cls.section_snapshot_map(previous_snapshot)
        desired_map = cls.section_snapshot_map(desired_sections)
        targeted_headings = cls.resolve_doc_update_targets(instruction, previous_snapshot or desired_sections)
        provisional_rename_map = cls.resolve_doc_rename_map(
            instruction,
            previous_snapshot=previous_snapshot,
            desired_sections=desired_sections,
            targeted_headings=targeted_headings,
            deleted_headings=[],
        )
        delete_headings = cls.resolve_doc_delete_targets(
            instruction,
            previous_snapshot=previous_snapshot,
            desired_sections=desired_sections,
            targeted_headings=targeted_headings,
            excluded_headings=list(provisional_rename_map.keys()),
        )
        rename_map = cls.resolve_doc_rename_map(
            instruction,
            previous_snapshot=previous_snapshot,
            desired_sections=desired_sections,
            targeted_headings=targeted_headings,
            deleted_headings=delete_headings,
        ) or provisional_rename_map
        changed_headings: list[str] = []
        for section in desired_sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            if not heading or heading in delete_headings:
                continue
            if targeted_headings and heading not in targeted_headings and heading not in rename_map.values():
                continue
            paragraphs = desired_map.get(heading, [])
            renamed_from = next((old for old, new in rename_map.items() if new == heading), None)
            baseline_heading = renamed_from or heading
            if previous_map.get(baseline_heading) != paragraphs:
                changed_headings.append(heading)
            elif renamed_from:
                changed_headings.append(heading)

        desired_sections = [section for section in desired_sections if str(section.get("heading") or "").strip() not in delete_headings]
        return {
            "desired_sections": desired_sections,
            "targeted_headings": targeted_headings,
            "changed_headings": changed_headings,
            "deleted_headings": delete_headings,
            "rename_map": rename_map,
        }

    @staticmethod
    def resolve_doc_delete_targets(
        instruction: str,
        *,
        previous_snapshot: list[dict],
        desired_sections: list[dict],
        targeted_headings: list[str],
        excluded_headings: list[str] | None = None,
    ) -> list[str]:
        if not instruction.strip():
            return []
        normalized_instruction = instruction.lower()
        delete_markers = ("删除", "删掉", "移除", "去掉", "不要保留", "去除", "delete", "remove", "drop")
        if not any(marker in instruction or marker in normalized_instruction for marker in delete_markers):
            return []
        previous_headings = {
            str(section.get("heading") or "").strip()
            for section in previous_snapshot
            if isinstance(section, dict) and str(section.get("heading") or "").strip()
        }
        excluded = {heading for heading in (excluded_headings or []) if heading}
        desired_headings = {
            str(section.get("heading") or "").strip()
            for section in desired_sections
            if isinstance(section, dict) and str(section.get("heading") or "").strip()
        }
        if targeted_headings:
            return [heading for heading in targeted_headings if heading in previous_headings and heading not in excluded]
        return [heading for heading in previous_headings if heading not in desired_headings and heading not in excluded]

    @staticmethod
    def resolve_doc_rename_map(
        instruction: str,
        *,
        previous_snapshot: list[dict],
        desired_sections: list[dict],
        targeted_headings: list[str],
        deleted_headings: list[str] | None = None,
    ) -> dict[str, str]:
        normalized_instruction = instruction.lower()
        rename_markers = ("重命名", "改名", "更名", "改成", "改为", "rename")
        if not any(marker in instruction or marker in normalized_instruction for marker in rename_markers):
            return {}
        deleted = {heading for heading in (deleted_headings or []) if heading}
        previous_headings = [
            str(section.get("heading") or "").strip()
            for section in previous_snapshot
            if isinstance(section, dict) and str(section.get("heading") or "").strip()
        ]
        desired_headings = [
            str(section.get("heading") or "").strip()
            for section in desired_sections
            if isinstance(section, dict) and str(section.get("heading") or "").strip()
        ]
        explicit_match = re.search(r"rename\s+(?P<old>.+?)\s+to\s+(?P<new>.+?)(?:[\.,;]| and |$)", normalized_instruction)
        if explicit_match:
            old_candidate = explicit_match.group("old").strip()
            new_candidate = explicit_match.group("new").strip()
            resolved_old = next((heading for heading in previous_headings if heading.lower() == old_candidate.lower()), "")
            resolved_new = next((heading for heading in desired_headings if heading.lower() == new_candidate.lower()), "")
            if resolved_old and resolved_new and resolved_old not in deleted and resolved_old != resolved_new:
                return {resolved_old: resolved_new}
        added_headings = [heading for heading in desired_headings if heading not in previous_headings]
        removed_headings = [heading for heading in previous_headings if heading not in desired_headings and heading not in deleted]
        if len(added_headings) != 1:
            return {}
        rename_targets = [heading for heading in targeted_headings if heading not in deleted]
        source_heading = rename_targets[0] if len(rename_targets) == 1 else (removed_headings[0] if len(removed_headings) == 1 else "")
        target_heading = added_headings[0]
        if not source_heading or source_heading == target_heading:
            return {}
        return {source_heading: target_heading}

    @staticmethod
    def format_current_document_context(current_doc: dict | None) -> str:
        if not isinstance(current_doc, dict) or not current_doc.get("document_id"):
            return ""
        lines = ["[当前协作文档]"]
        title = str(current_doc.get("title") or "").strip()
        if title:
            lines.append(f"标题：{title}")
        version = current_doc.get("version")
        if version:
            lines.append(f"版本：v{version}")
        url = str(current_doc.get("url") or "").strip()
        if url:
            lines.append(f"链接：{url}")
        snapshot = current_doc.get("section_snapshot")
        if isinstance(snapshot, list) and snapshot:
            lines.append("章节快照：")
            for section in snapshot:
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("heading") or "未命名章节").strip()
                paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
                lines.append(f"- {heading}")
                for paragraph in paragraphs[:4]:
                    text = str(paragraph).strip()
                    if text:
                        lines.append(f"  - {text}")
        return "\n".join(lines)

    @staticmethod
    def build_document_revision_instruction(instruction: str, *, current_doc: dict | None = None) -> str:
        title = str((current_doc or {}).get("title") or "").strip()
        version = (current_doc or {}).get("version")
        current_note = ""
        if title:
            current_note = f"当前文档：{title}"
            if version:
                current_note += f"（v{version}）"
            current_note += "。"
        return (
            "请基于当前协作文档执行一次文档修订。"
            "优先复用已有文档结构，只输出需要更新后的文档内容；"
            "如果用户指定了章节，请只改相关章节。"
            f"{current_note}\n\n"
            f"用户修订要求：{instruction}"
        )

    @staticmethod
    def build_doc_update_header_lines(*, instruction: str, targeted_headings: list[str]) -> list[str]:
        lines = [f"Trigger: {instruction.strip() or 'supplement collaborative document'}"]
        if targeted_headings:
            lines.append(f"Target sections: {', '.join(targeted_headings)}")
        return lines

    @staticmethod
    def resolve_doc_update_targets(instruction: str, sections: list[dict]) -> list[str]:
        normalized_instruction = instruction.strip().lower()
        if not normalized_instruction:
            return []

        explicit_matches: list[str] = []
        available_headings = [
            str(section.get("heading") or "").strip()
            for section in sections
            if isinstance(section, dict) and str(section.get("heading") or "").strip()
        ]
        for heading in available_headings:
            if heading and heading.lower() in normalized_instruction:
                explicit_matches.append(heading)
        if explicit_matches:
            return explicit_matches

        keyword_map = {
            "文档说明": ("主题", "场景", "说明", "受众"),
            "讨论摘要": ("摘要", "总结", "背景", "概述", "说明"),
            "任务清单": ("任务", "待办", "todo", "负责人", "排期"),
            "风险与卡点": ("风险", "卡点", "阻塞", "问题"),
            "下一步建议": ("下一步", "建议", "行动项", "跟进"),
            "演示重点": ("重点", "亮点", "强调"),
            "建议补充素材": ("素材", "图片", "图表", "补充素材"),
        }
        targeted: list[str] = []
        for heading in available_headings:
            keywords = keyword_map.get(heading)
            if keywords and any(keyword in normalized_instruction for keyword in keywords):
                targeted.append(heading)
        return targeted

    @staticmethod
    def section_snapshot_map(snapshot: list[dict]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for section in snapshot:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
            result[heading] = cleaned
        return result

    @staticmethod
    def merge_doc_section_snapshots(
        previous_snapshot: list[dict],
        updated_sections: list[dict],
        *,
        deleted_headings: list[str] | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> list[dict[str, list[str]]]:
        deleted = {heading for heading in (deleted_headings or []) if heading}
        rename_map = {str(old).strip(): str(new).strip() for old, new in (rename_map or {}).items() if str(old).strip() and str(new).strip()}
        merged: dict[str, list[str]] = {}
        order: list[str] = []

        for section in previous_snapshot:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            heading = rename_map.get(heading, heading)
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
            if (not heading and not cleaned) or heading in deleted:
                continue
            if heading not in merged:
                order.append(heading)
            merged[heading] = cleaned

        for section in updated_sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
            cleaned = [str(item).strip() for item in paragraphs if str(item).strip()]
            if (not heading and not cleaned) or heading in deleted:
                continue
            if heading not in merged:
                order.append(heading)
            merged[heading] = cleaned

        return [{"heading": heading, "paragraphs": merged[heading]} for heading in order if merged.get(heading)]

    @staticmethod
    def build_doc_sync_lines(
        mode: str,
        document_info: dict,
        *,
        appended_block_count: int | None = None,
        replaced_block_count: int | None = None,
        inserted_block_count: int | None = None,
        changed_headings: list[str] | None = None,
        appended_headings: list[str] | None = None,
        deleted_headings: list[str] | None = None,
        renamed_headings: list[str] | None = None,
        patched_headings: list[str] | None = None,
        folder_scope: str | None = None,
        folder_url: str | None = None,
        folder_note: str | None = None,
    ) -> list[str]:
        title = str(document_info.get("title") or "collab_doc").strip()
        url = str(document_info.get("url") or "").strip() or None
        version = document_info.get("version")

        if mode == "updated":
            lines = [f"- Updated document: {title}"]
            if replaced_block_count is not None or inserted_block_count is not None:
                lines.append(f"- Replaced blocks: {replaced_block_count or 0}")
                lines.append(f"- Inserted blocks: {inserted_block_count or 0}")
            elif appended_block_count is not None:
                lines.append(f"- Appended blocks: {appended_block_count}")
            if changed_headings:
                lines.append(f"- Updated sections: {', '.join(changed_headings)}")
            if appended_headings:
                lines.append(f"- Appended new sections: {', '.join(str(item) for item in appended_headings)}")
            if deleted_headings:
                lines.append(f"- Deleted sections: {', '.join(str(item) for item in deleted_headings)}")
            if renamed_headings:
                lines.append(f"- Renamed sections: {', '.join(str(item) for item in renamed_headings)}")
            if patched_headings:
                lines.append(f"- Patched section bodies: {', '.join(str(item) for item in patched_headings)}")
            lines.append("- Update strategy: patched matched section bodies in place, with delete/rename support when requested.")
            if version is not None:
                lines.append(f"- Current version: v{version}")
        elif mode == "noop":
            lines = [f"- No content changes detected. Keep current document: {title}"]
            if changed_headings:
                lines.append(f"- Requested sections are already up to date: {', '.join(changed_headings)}")
            if version is not None:
                lines.append(f"- Current version: v{version}")
        else:
            lines = [f"- Created document: {title}"]
            if version is not None:
                lines.append(f"- Current version: v{version}")
            if folder_scope == "explicit" and folder_url:
                lines.append(f"- Output folder: {folder_url}")
            if folder_note:
                lines.append(f"- {folder_note}")

        if url:
            lines.append(f"- Document URL: {url}")
        return lines

    @staticmethod
    def _doc_title_timestamp() -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
