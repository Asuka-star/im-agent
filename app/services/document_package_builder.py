from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.services.artifact_skills import DocSkill
from app.services.doc_tool import DocTool


class DocumentPackageBuilder:
    """Builds structured document packages from agent outputs."""

    def __init__(self) -> None:
        self.doc_skill = DocSkill()

    def package_from_llm_result(
        self,
        *,
        instruction: str,
        llm_result: dict,
        stats_as_of: str | None = None,
    ) -> dict | None:
        provided = llm_result.get("doc") if isinstance(llm_result, dict) else None
        if not isinstance(provided, dict):
            return None
        sections = provided.get("sections")
        if not isinstance(sections, list) or not sections:
            return None
        base_title = str(provided.get("title") or "").strip() or self.default_title(
            instruction,
            stats_as_of=stats_as_of,
        )
        package = {
            "title": self.compose_title(base_title, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": DocTool.normalize_doc_sections(sections),
        }
        edit_plan = (
            llm_result.get("artifact_edit_plan")
            or llm_result.get("edit_plan")
            or provided.get("artifact_edit_plan")
            or provided.get("edit_plan")
        )
        if isinstance(edit_plan, dict):
            package["artifact_edit_plan"] = edit_plan
        return self.doc_skill.normalize(package)

    @staticmethod
    def is_outline_request(instruction: str) -> bool:
        return any(keyword in instruction for keyword in ("汇报", "路演", "大纲", "PPT", "ppt", "演示"))

    def from_analysis(self, analysis: AnalyzeResponse, instruction: str, *, stats_as_of: str | None = None) -> dict:
        sections = [
            {
                "heading": "讨论摘要",
                "paragraphs": [analysis.summary],
            }
        ]
        if analysis.tasks:
            sections.append(
                {
                    "heading": "任务清单",
                    "paragraphs": [
                        f"{idx}. {task.title}｜负责人：{task.owner}｜截止：{task.due_date}｜优先级：{task.priority}"
                        for idx, task in enumerate(analysis.tasks, start=1)
                    ],
                }
            )
        if analysis.risks:
            sections.append(
                {
                    "heading": "风险与卡点",
                    "paragraphs": [f"{idx}. {risk}" for idx, risk in enumerate(analysis.risks, start=1)],
                }
            )
        if analysis.next_actions:
            sections.append(
                {
                    "heading": "下一步建议",
                    "paragraphs": [f"{idx}. {item}" for idx, item in enumerate(analysis.next_actions, start=1)],
                }
            )
        return self.doc_skill.normalize({
            "title": self.default_title(instruction, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": DocTool.normalize_doc_sections(sections),
        })

    def from_presentation(self, package: dict, instruction: str, *, stats_as_of: str | None = None) -> dict:
        theme = str(package.get("theme") or "汇报大纲").strip()
        audience = str(package.get("audience") or "团队协作汇报").strip()
        slides = package.get("slides") if isinstance(package.get("slides"), list) else []
        emphasis = package.get("emphasis") if isinstance(package.get("emphasis"), list) else []
        assets = package.get("assets") if isinstance(package.get("assets"), list) else []

        sections = [
            {
                "heading": "文档说明",
                "paragraphs": [f"主题：{theme}", f"适用场景：{audience}"],
            }
        ]
        for index, slide in enumerate(slides[:7], start=1):
            if not isinstance(slide, dict):
                continue
            title = str(slide.get("title") or f"P{index}").strip()
            bullets = slide.get("bullets") if isinstance(slide.get("bullets"), list) else []
            sections.append(
                {
                    "heading": f"P{index}. {title}",
                    "paragraphs": [str(item).strip() for item in bullets if str(item).strip()],
                }
            )
        if emphasis:
            sections.append(
                {
                    "heading": "演示重点",
                    "paragraphs": [str(item).strip() for item in emphasis if str(item).strip()],
                }
            )
        if assets:
            sections.append(
                {
                    "heading": "建议补充素材",
                    "paragraphs": [str(item).strip() for item in assets if str(item).strip()],
                }
            )
        return self.doc_skill.normalize({
            "title": self.default_title(instruction, fallback=theme, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": DocTool.normalize_doc_sections(sections),
        })

    def default_title(self, instruction: str, fallback: str | None = None, stats_as_of: str | None = None) -> str:
        timestamp = stats_as_of or self.title_timestamp()
        if fallback:
            return self.compose_title(f"{settings.feishu_doc_title_prefix} - {fallback.strip()}", stats_as_of=timestamp)
        condensed = " ".join((instruction or "").split()).strip()
        if condensed:
            condensed = condensed[:24]
            return self.compose_title(f"{settings.feishu_doc_title_prefix} - {condensed}", stats_as_of=timestamp)
        return self.compose_title(f"{settings.feishu_doc_title_prefix} - 讨论整理", stats_as_of=timestamp)

    @staticmethod
    def title_timestamp() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def compose_title(base_title: str, *, stats_as_of: str | None) -> str:
        title = base_title.strip()
        if not title:
            title = settings.feishu_doc_title_prefix
        if "统计至" in title:
            return title
        if stats_as_of:
            return f"{title} - 统计至{stats_as_of}"
        return title
