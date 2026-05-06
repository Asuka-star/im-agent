from __future__ import annotations

import re


class ArtifactTitleService:
    """Derives formal artifact titles from requirement/document context."""

    GENERIC_ARTIFACT_TITLES = {
        "canvas",
        "flow",
        "flowchart",
        "diagram",
        "whiteboard",
        "presentation",
        "slides",
        "slide deck",
        "ppt",
        "ppt演示稿",
        "演示稿",
        "汇报演示稿",
        "答辩演示稿",
        "汇报大纲",
        "协作演示稿",
        "基于飞书群聊讨论的需求方案与正式汇报",
        "流程图",
        "产品流程图",
        "业务流程图",
        "用户流程图",
        "需求流程图",
        "白板",
        "画布",
        "模块图",
        "架构图",
        "风险图",
        "风险应对图",
    }

    DOC_TITLE_PREFIXES = ("协同产出", "协作产出", "需求方案", "正式需求方案文档")

    @classmethod
    def canvas_title(
        cls,
        *,
        current_title: str | None,
        instruction: str,
        workspace_context: str,
        template: str,
    ) -> str:
        subject = cls.requirement_subject(workspace_context, instruction=instruction)
        suffix = cls._canvas_suffix(template, instruction)
        if subject:
            return cls._join_subject_suffix(subject, suffix)
        title = cls._clean_title(current_title)
        if title and not cls._is_generic_or_instruction_title(title, instruction):
            return title
        return suffix

    @classmethod
    def presentation_title(
        cls,
        *,
        current_title: str | None,
        instruction: str,
        workspace_context: str,
    ) -> str | None:
        subject = cls.requirement_subject(workspace_context, instruction=instruction)
        suffix = cls._presentation_suffix(instruction)
        if subject:
            return cls._join_subject_suffix(subject, suffix)
        title = cls._clean_title(current_title)
        if title and not cls._is_generic_or_instruction_title(title, instruction):
            return title
        return suffix

    @classmethod
    def requirement_subject(cls, workspace_context: str, *, instruction: str = "") -> str:
        text = str(workspace_context or "")
        candidates: list[str] = []
        candidates.extend(cls._subjects_from_document_titles(text))
        candidates.extend(cls._subjects_from_project_phrases(text))
        candidates.extend(cls._subjects_from_project_phrases(instruction))
        for candidate in candidates:
            normalized = cls._normalize_subject(candidate)
            if normalized and normalized not in cls.GENERIC_ARTIFACT_TITLES:
                return normalized
        return ""

    @classmethod
    def _subjects_from_document_titles(cls, text: str) -> list[str]:
        candidates: list[str] = []
        patterns = (
            r"(?:标题|文档标题|当前文档)[:：]\s*(.+)",
            r"-\s*(.+?(?:需求方案|正式需求方案文档|需求文档|方案文档))\s*$",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                candidates.append(match.group(1).strip())
        return candidates

    @staticmethod
    def _subjects_from_project_phrases(text: str) -> list[str]:
        candidates: list[str] = []
        patterns = (
            r"(?:想做|要做|做一个|建设|开发|打造|本次项目(?:是|为)?|项目(?:名称|主题)?[:：])\s*(?:一个|一套|一份)?\s*([^，。；;\n]+?(?:系统|平台|工具|应用|方案|Agent|机器人))",
            r"([^，。；;\n]{4,40}?(?:报名与审核系统|管理系统|协作系统|展示系统|审核系统|报名系统))",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, str(text or ""), flags=re.IGNORECASE):
                candidates.append(match.group(1).strip())
        return candidates

    @classmethod
    def _normalize_subject(cls, value: str) -> str:
        text = cls._clean_title(value)
        if not text:
            return ""
        for prefix in cls.DOC_TITLE_PREFIXES:
            text = re.sub(rf"^{re.escape(prefix)}\s*[-—:：]\s*", "", text).strip()
        text = re.sub(r"\s*[-—]\s*统计至.+$", "", text).strip()
        text = re.sub(r"(?:正式)?需求方案(?:文档)?$", "", text).strip()
        text = re.sub(r"(?:需求文档|方案文档)$", "", text).strip()
        text = re.sub(r"(?:答辩演示稿|汇报演示稿|演示稿|需求流程图|产品流程图|业务流程图|流程图|技术架构图|风险应对图)$", "", text).strip()
        text = text.strip("-—:： ，。")
        return text[:40]

    @staticmethod
    def _clean_title(value: str | None) -> str:
        text = " ".join(str(value or "").split()).strip()
        text = re.sub(r"^[@\w_]+\s+", "", text).strip()
        return text.strip("-—:： ，。")

    @classmethod
    def _is_generic_or_instruction_title(cls, title: str, instruction: str) -> bool:
        normalized = cls._clean_title(title).lower()
        if normalized in cls.GENERIC_ARTIFACT_TITLES:
            return True
        instruction_text = cls._clean_title(instruction)
        if instruction_text and normalized == instruction_text.lower():
            return True
        return bool(re.search(r"(根据|基于|生成|画|整理|刚才|这份|需求).*?(流程图|ppt|演示稿|画布|canvas)", title, flags=re.IGNORECASE))

    @staticmethod
    def _canvas_suffix(template: str, instruction: str) -> str:
        text = str(instruction or "")
        if str(template or "").lower() == "risk" or re.search(r"(风险|应对|risk)", text, flags=re.IGNORECASE):
            return "风险应对图"
        if str(template or "").lower() == "module" or re.search(r"(架构|模块|architecture|module)", text, flags=re.IGNORECASE):
            return "技术架构图"
        if re.search(r"(产品流程|业务流程|用户流程|需求|flow|process)", text, flags=re.IGNORECASE):
            return "需求流程图"
        return "协作流程图"

    @staticmethod
    def _presentation_suffix(instruction: str) -> str:
        text = str(instruction or "")
        if re.search(r"(答辩|评委|路演|比赛|竞赛|defen[cs]e|pitch)", text, flags=re.IGNORECASE):
            return "答辩演示稿"
        return "汇报演示稿"

    @staticmethod
    def _join_subject_suffix(subject: str, suffix: str) -> str:
        if subject.endswith(suffix):
            return subject
        if suffix and suffix in subject:
            return subject
        return f"{subject}{suffix}"
