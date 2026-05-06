from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.schemas.analyze import AnalyzeResponse
from app.services.artifact_skills import DocSkill
from app.services.tools.doc_tool import DocTool


class DocumentPackageBuilder:
    """Builds structured document packages from agent outputs."""

    TASK_LIST_MARKERS = ("任务清单", "待办", "TODO", "todo", "分工清单", "负责人清单")
    TASK_LIST_ONLY_MARKERS = ("只整理任务", "只要任务", "仅整理任务", "只做任务", "只输出任务", "任务清单文档", "待办文档")
    SOLUTION_DOCUMENT_MARKERS = ("需求文档", "方案文档", "正式文档", "答辩材料", "演示文稿", "需求方案", "产品方案")

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
        if not self.is_task_list_document_request(instruction):
            return self.from_analysis_as_solution_document(
                analysis,
                instruction,
                stats_as_of=stats_as_of,
            )

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

    def from_discussion_text(self, source_text: str, instruction: str, *, stats_as_of: str | None = None) -> dict | None:
        """Build a requirement/solution document from raw IM discussion facts.

        This is the local fallback for lifecycle documents. It intentionally uses
        the original discussion sentences rather than task snapshots, because a
        requirement discussion often has no explicit assignees yet.
        """
        if self.is_task_list_document_request(instruction):
            return None

        sentences = self._discussion_sentences(source_text)
        if not sentences:
            return None

        buckets = self._bucket_requirement_sentences(sentences)
        if not any(buckets[key] for key in ("background", "users", "flow", "scope", "risks")):
            return None

        title = self._title_from_discussion(sentences, instruction, stats_as_of=stats_as_of)
        sections: list[dict] = [
            {
                "heading": "文档说明",
                "paragraphs": [
                    "本文档由当前 IM 讨论沉淀而成，用于后续方案评审、演示稿生成和交付跟进。",
                ],
            },
            {
                "heading": "背景与痛点",
                "paragraphs": buckets["background"] or sentences[:2],
            },
            {
                "heading": "目标用户与使用场景",
                "paragraphs": buckets["users"] or ["围绕讨论中提到的学生、负责人、老师等角色继续补充典型使用场景。"],
            },
            {
                "heading": "核心需求与方案范围",
                "paragraphs": self._core_requirement_lines(buckets, sentences),
            },
            {
                "heading": "产品流程",
                "paragraphs": buckets["flow"] or ["从用户进入系统、提交信息、审核处理到结果沉淀形成闭环。"],
            },
            {
                "heading": "技术方案与数据设计",
                "paragraphs": self._technical_solution_lines(buckets, sentences),
            },
        ]
        if buckets["risks"]:
            sections.append(
                {
                    "heading": "风险与待确认",
                    "paragraphs": buckets["risks"],
                }
            )
        sections.append(
            {
                "heading": "里程碑与下一步",
                "paragraphs": self._milestone_lines(buckets, sentences),
            }
        )
        sections.append(
            {
                "heading": "演示重点",
                "paragraphs": [
                    "优先讲清用户痛点、目标角色、核心流程、一期范围、风险控制和后续演进。",
                    "任务分工只作为实施计划补充，不作为这份需求方案文档的主线。",
                ],
            }
        )
        return self.doc_skill.normalize(
            {
                "title": title,
                "stats_as_of": stats_as_of,
                "sections": DocTool.normalize_doc_sections(sections),
            }
        )

    def from_requirement_brief(
        self,
        brief: dict,
        instruction: str,
        *,
        stats_as_of: str | None = None,
    ) -> dict | None:
        """Build a lifecycle document from structured requirement facts."""
        if self.is_task_list_document_request(instruction) or not isinstance(brief, dict):
            return None

        problem = self._brief_list(brief, "problem")
        target_users = self._brief_list(brief, "target_users")
        goals = self._brief_list(brief, "goals")
        product_flow = self._brief_list(brief, "product_flow")
        technical_notes = self._brief_list(brief, "technical_notes")
        risks = self._brief_list(brief, "risks")
        open_questions = self._brief_list(brief, "open_questions")
        evidence = self._brief_list(brief, "source_evidence")
        scope = brief.get("scope") if isinstance(brief.get("scope"), dict) else {}
        scope_lines = [
            *self._brief_scope_lines(scope, "phase_one", "一期范围"),
            *self._brief_scope_lines(scope, "phase_later", "后续范围"),
            *self._brief_scope_lines(scope, "out_of_scope", "暂不纳入"),
        ]
        implementation_lines = self._brief_implementation_lines(brief.get("implementation_plan"))

        if not any([problem, target_users, goals, product_flow, scope_lines, technical_notes, risks, open_questions]):
            return None

        base_title = str(brief.get("title") or "").strip()
        if base_title:
            if settings.feishu_doc_title_prefix not in base_title:
                base_title = f"{settings.feishu_doc_title_prefix} - {base_title}"
            title = self.compose_title(base_title, stats_as_of=stats_as_of)
        else:
            title = self.default_title(instruction, stats_as_of=stats_as_of)

        sections: list[dict] = [
            {
                "heading": "文档说明",
                "paragraphs": self._unique_lines(
                    [
                        "本文档由 IM 需求讨论结构化沉淀而成，可继续用于演示稿、流程图和交付包生成。",
                        *[f"依据：{item}" for item in evidence[:3]],
                    ]
                ),
            },
            {
                "heading": "背景与痛点",
                "paragraphs": problem or goals[:2] or ["当前讨论正在明确需求背景、业务痛点和目标价值。"],
            },
            {
                "heading": "目标用户与使用场景",
                "paragraphs": target_users or ["继续补充核心用户角色、使用场景和关键诉求。"],
            },
            {
                "heading": "核心目标",
                "paragraphs": goals or ["继续明确本次方案需要达成的产品目标和验收口径。"],
            },
            {
                "heading": "方案范围",
                "paragraphs": scope_lines or ["继续拆分一期范围、后续范围和暂不纳入内容。"],
            },
            {
                "heading": "产品流程",
                "paragraphs": product_flow or ["继续补齐用户进入、提交、处理、反馈和数据沉淀的完整流程。"],
            },
            {
                "heading": "技术方案与数据设计",
                "paragraphs": technical_notes
                or ["围绕核心流程补充模块划分、数据结构、权限边界、异常处理和容量保障策略。"],
            },
        ]
        risk_lines = [
            *[f"风险：{item}" for item in risks],
            *[f"待确认：{item}" for item in open_questions],
        ]
        if risk_lines:
            sections.append({"heading": "风险与待确认", "paragraphs": self._unique_lines(risk_lines)})
        if implementation_lines:
            sections.append({"heading": "实施计划与分工", "paragraphs": implementation_lines})
        sections.append(
            {
                "heading": "演示重点",
                "paragraphs": [
                    "正式演示稿应优先讲清用户痛点、目标用户、核心目标、产品流程、一期范围、风险控制和交付成果。",
                    "任务分工只作为实施计划支撑，不作为整体汇报主线。",
                ],
            }
        )
        return self.doc_skill.normalize(
            {
                "title": title,
                "stats_as_of": stats_as_of,
                "sections": DocTool.normalize_doc_sections(sections),
            }
        )

    def from_analysis_as_solution_document(
        self,
        analysis: AnalyzeResponse,
        instruction: str,
        *,
        stats_as_of: str | None = None,
    ) -> dict:
        task_lines = [
            f"{idx}. {task.title}｜负责人：{task.owner}｜截止：{task.due_date}｜优先级：{task.priority}"
            for idx, task in enumerate(analysis.tasks, start=1)
        ]
        requirement_lines = [task.title for task in analysis.tasks[:4]]
        next_action_lines = [str(item).strip() for item in analysis.next_actions if str(item).strip()]
        sections = [
            {
                "heading": "背景与痛点",
                "paragraphs": [analysis.summary or "本轮讨论正在沉淀需求背景、用户问题与协作目标。"],
            },
            {
                "heading": "核心需求与方案范围",
                "paragraphs": requirement_lines
                or next_action_lines[:3]
                or ["围绕当前 IM 讨论继续明确目标用户、核心场景、功能边界和二期范围。"],
            },
            {
                "heading": "产品流程与技术方案",
                "paragraphs": [
                    "从 IM 讨论收集需求与约束，沉淀为可持续迭代的方案文档。",
                    "围绕文档继续补充流程图、演示稿和交付包，形成正式汇报材料。",
                    "关键修改通过 Agent 运行态、确认节点和产物版本记录追踪。",
                ],
            },
        ]
        if analysis.risks:
            sections.append(
                {
                    "heading": "风险与约束",
                    "paragraphs": [f"{idx}. {risk}" for idx, risk in enumerate(analysis.risks, start=1)],
                }
            )
        if task_lines:
            sections.append(
                {
                    "heading": "实施计划与分工",
                    "paragraphs": task_lines,
                }
            )
        if next_action_lines:
            sections.append(
                {
                    "heading": "里程碑与下一步",
                    "paragraphs": [f"{idx}. {item}" for idx, item in enumerate(next_action_lines, start=1)],
                }
            )
        sections.append(
            {
                "heading": "演示稿准备要点",
                "paragraphs": [
                    "正式演示稿应优先讲清用户痛点、核心需求、产品流程、技术方案、风险控制和交付成果。",
                    "任务分工只作为实施计划支撑，不作为整体汇报主线。",
                ],
            }
        )
        return self.doc_skill.normalize({
            "title": self.default_title(instruction, stats_as_of=stats_as_of),
            "stats_as_of": stats_as_of,
            "sections": DocTool.normalize_doc_sections(sections),
        })

    @classmethod
    def is_task_list_document_request(cls, instruction: str) -> bool:
        text = str(instruction or "")
        if not any(marker in text for marker in cls.TASK_LIST_MARKERS):
            return False
        if any(marker in text for marker in cls.SOLUTION_DOCUMENT_MARKERS):
            return any(marker in text for marker in cls.TASK_LIST_ONLY_MARKERS)
        return True

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

    def _discussion_sentences(self, source_text: str) -> list[str]:
        lines: list[str] = []
        for raw_line in str(source_text or "").splitlines():
            line = raw_line.strip().lstrip("-").strip()
            if not line or (line.startswith("[") and line.endswith("]")):
                continue
            content_match = re.search(r"(?:内容|content)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
            if content_match:
                line = content_match.group(1).strip()
            line = re.sub(r"^发言人\s*[:：][^|]+\|\s*", "", line).strip()
            if line:
                lines.append(line)

        sentences: list[str] = []
        for line in lines:
            parts = [part.strip(" ，,；;。") for part in re.split(r"(?<=[。！？!?；;])\s*", line) if part.strip()]
            if len(parts) <= 1:
                clauses = [line.strip()]
            else:
                clauses = parts
            for clause in clauses:
                text = clause.strip()
                if text and text not in sentences:
                    sentences.append(text[:240])
        return sentences[:24]

    def _bucket_requirement_sentences(self, sentences: list[str]) -> dict[str, list[str]]:
        buckets = {
            "background": [],
            "users": [],
            "flow": [],
            "scope": [],
            "risks": [],
            "tech": [],
        }
        markers = {
            "background": ("想做", "主要解决", "痛点", "问题", "背景", "目标是"),
            "users": ("目标用户", "学生", "负责人", "老师", "用户", "学院"),
            "flow": ("核心流程", "流程", "查看", "提交", "报名", "审核", "通过", "名单", "统计结果"),
            "scope": ("先做", "核心能力", "一期", "二期", "范围", "放到"),
            "risks": ("风险", "压力", "高峰", "确认", "待确认", "规则", "阻塞"),
            "tech": ("接口", "数据", "看板", "导出", "提醒", "候补", "统计"),
        }
        for sentence in sentences:
            for key, keywords in markers.items():
                if any(keyword in sentence for keyword in keywords):
                    buckets[key].append(sentence)
        return {key: self._unique_lines(value) for key, value in buckets.items()}

    def _core_requirement_lines(self, buckets: dict[str, list[str]], sentences: list[str]) -> list[str]:
        lines = [*buckets["scope"]]
        for sentence in sentences:
            if any(keyword in sentence for keyword in ("报名", "审核", "名单导出", "数据看板", "候补队列", "风险提醒")):
                lines.append(sentence)
        return self._unique_lines(lines) or ["继续明确一期核心能力、二期能力边界和验收口径。"]

    def _technical_solution_lines(self, buckets: dict[str, list[str]], sentences: list[str]) -> list[str]:
        lines = [*buckets["tech"]]
        if any("报名" in sentence for sentence in sentences):
            lines.append("报名模块需要承载活动列表、报名表单、报名状态和名单数据。")
        if any("审核" in sentence for sentence in sentences):
            lines.append("审核模块需要支持负责人统一处理报名信息，并将通过结果沉淀为名单。")
        if any(keyword in " ".join(sentences) for keyword in ("统计", "数据", "风险提醒")):
            lines.append("数据模块面向老师提供统计结果、风险提醒和后续运营分析。")
        if any(keyword in " ".join(sentences) for keyword in ("接口压力", "高峰")):
            lines.append("报名高峰期需要关注接口限流、排队、缓存或异步处理等容量保障策略。")
        return self._unique_lines(lines) or ["技术栈、数据库、部署方式和系统集成方案未在讨论中明确，需后续确认。"]

    def _milestone_lines(self, buckets: dict[str, list[str]], sentences: list[str]) -> list[str]:
        lines = []
        if buckets["scope"]:
            lines.extend(buckets["scope"])
        if any("审核规则" in sentence or "老师确认" in sentence for sentence in sentences):
            lines.append("下一步需要和老师确认活动审核规则、风险提醒口径和数据查看权限。")
        if not lines:
            lines.append("下一步补齐核心流程细节、验收标准、风险处置方案和演示素材。")
        return self._unique_lines(lines)

    def _title_from_discussion(self, sentences: list[str], instruction: str, *, stats_as_of: str | None) -> str:
        joined = " ".join(sentences[:3])
        match = re.search(r"(?:做一个|做一套|建设|开发)([^，。；;]+?系统)", joined)
        if match:
            return self.compose_title(f"{settings.feishu_doc_title_prefix} - {match.group(1).strip()}需求方案", stats_as_of=stats_as_of)
        return self.default_title(instruction, stats_as_of=stats_as_of)

    @classmethod
    def _brief_list(cls, brief: dict, key: str) -> list[str]:
        value = brief.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return cls._unique_lines([str(item).strip() for item in value if str(item).strip()])

    @classmethod
    def _brief_scope_lines(cls, scope: dict, key: str, label: str) -> list[str]:
        items = cls._brief_list(scope, key)
        return [f"{label}：{item}" for item in items]

    @classmethod
    def _brief_implementation_lines(cls, items: object) -> list[str]:
        if not isinstance(items, list):
            return []
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("item") or item.get("title") or "").strip()
            owner = str(item.get("owner") or "").strip()
            due_date = str(item.get("due_date") or item.get("due") or "").strip()
            has_assignment = owner and owner.upper() != "TBD"
            has_due_date = due_date and due_date.upper() != "TBD"
            if not title or not (has_assignment or has_due_date):
                continue
            parts = [title]
            if has_assignment:
                parts.append(f"负责人：{owner}")
            if has_due_date:
                parts.append(f"截止：{due_date}")
            lines.append("｜".join(parts))
        return cls._unique_lines(lines)

    @staticmethod
    def _unique_lines(lines: list[str]) -> list[str]:
        result: list[str] = []
        for line in lines:
            text = " ".join(str(line or "").split()).strip()
            if text and text not in result:
                result.append(text)
        return result
