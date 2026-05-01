from __future__ import annotations

from typing import Any

from app.schemas.analyze import AnalyzeResponse


class ResponseFormatter:
    """Formats user-facing IM replies from workflow payloads."""

    def combine_plan_replies(self, reply_parts: list[str]) -> str | None:
        cleaned = [part.strip() for part in reply_parts if str(part).strip()]
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return cleaned[0]
        return "\n\n".join(cleaned)

    def format_clarification_reply(self, *, intent: str, clarification: dict) -> str:
        label = {
            "doc": "文档",
            "slides": "演示稿",
            "summary": "讨论总结",
            "tasks": "任务整理",
            "risks": "风险判断",
            "status": "状态回答",
        }.get(intent, "协作处理")
        lines = [f"【Agent 需要再确认一下】({label})", clarification["question"]]
        reason = str(clarification.get("reason") or "").strip()
        if reason:
            lines.append(f"原因：{reason}")
        options = clarification.get("options") or []
        if options:
            lines.append("可选方案：")
            for index, option in enumerate(options, start=1):
                lines.append(f"{index}. {option}")
        lines.append("你可以在工作台里直接确认，或继续回复我更具体的要求。")
        return "\n".join(lines)

    def format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        lines = ["【文档同步】", f"标题：{str(package.get('title') or '协同文档').strip()}"]
        if sections:
            lines.append("正文结构：")
            for index, section in enumerate(sections[:6], start=1):
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("heading") or f"部分 {index}").strip()
                paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
                lines.append(f"{index}. {heading}")
                for paragraph in paragraphs[:2]:
                    content = str(paragraph).strip()
                    if content:
                        lines.append(f"- {content}")
        lines.append("同步结果：")
        lines.extend(sync_lines)
        return "\n".join(lines)

    def format_analysis_reply(
        self,
        analysis: AnalyzeResponse,
        mode: str,
    ) -> str:
        header = {
            "summary": "【讨论总结】",
            "tasks": "【待办清单】",
            "risks": "【风险与卡点】",
        }.get(mode, "【协作整理】")

        lines = [header, f"摘要：{analysis.summary}"]

        if mode in {"summary", "tasks"}:
            lines.append("任务：")
            if analysis.tasks:
                for idx, task in enumerate(analysis.tasks, start=1):
                    lines.append(
                        f"{idx}. {task.title} | 负责人：{task.owner} | 截止：{task.due_date} | 优先级：{task.priority}"
                    )
            else:
                lines.append("1. 当前讨论还没有形成明确待办。")

        if mode in {"summary", "risks"}:
            lines.append("风险：")
            if analysis.risks:
                for idx, risk in enumerate(analysis.risks, start=1):
                    lines.append(f"{idx}. {risk}")
            else:
                lines.append("1. 当前没有识别到新的显性风险。")

        lines.append("下一步建议：")
        for idx, action in enumerate(analysis.next_actions, start=1):
            lines.append(f"{idx}. {action}")

        return "\n".join(lines)

    def format_status_reply(self, query: str, tasks: list[Any], payload: dict) -> str:
        if "风险" in query or "卡点" in query or "阻塞" in query:
            risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
            if not risks:
                risks = ["当前没有额外记录到新的风险项。"]
            lines = ["【当前风险】"]
            for idx, risk in enumerate(risks, start=1):
                lines.append(f"{idx}. {risk}")
            return "\n".join(lines)

        if "总结" in query or "结论" in query or "摘要" in query or "概览" in query:
            summary = str(payload.get("summary") or "").strip()
            if not summary:
                return "我这边还没有现成的讨论总结。你可以先让我总结一下当前讨论。"
            return "\n".join(["【当前总结】", summary])

        if not tasks:
            return "我这边还没有现成的任务快照。你可以先让我总结一下或整理待办，我再基于结果回答状态问题。"

        if "没负责人" in query or "未分配" in query:
            pending = [task for task in tasks if task.owner == "TBD"]
            if not pending:
                return "【负责人检查】\n当前任务都已经有明确负责人，没有未分配项。"
            lines = ["【负责人检查】", "以下任务还没有明确负责人："]
            for idx, task in enumerate(pending, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        if "谁负责" in query:
            lines = ["【当前分工】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} -> {task.owner}")
            return "\n".join(lines)

        if "截止" in query or "到期" in query:
            lines = ["【时间节点】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {task.title} | 截止：{task.due_date}")
            return "\n".join(lines)

        completed = sum(1 for task in tasks if str(task.status).lower() == "done")
        unassigned = sum(1 for task in tasks if task.owner == "TBD")
        return "\n".join(
            [
                "【当前协作状态】",
                f"- 任务总数：{len(tasks)}",
                f"- 已完成：{completed}",
                f"- 待确认负责人：{unassigned}",
                "- 如需更具体输出，可以继续问：谁负责什么 / 哪些任务没负责人 / 当前有什么风险。",
            ]
        )

    def format_help_reply(self, reason: str | None = None) -> str:
        lines = ["【我可以这样帮你】"]
        if reason:
            lines.append(f"提示：{reason}")
        lines.extend(
            [
                "- @我 总结一下这次讨论",
                "- @我 帮我整理待办",
                "- @我 看一下当前风险和卡点",
                "- @我 现在还有哪些任务没负责人",
                "- @我 帮我搞个汇报大纲",
            ]
        )
        return "\n".join(lines)
