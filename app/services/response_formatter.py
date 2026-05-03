from __future__ import annotations

from typing import Any

from app.schemas.analyze import AnalyzeResponse
from app.schemas.next_action import NextActionBundle


class ResponseFormatter:
    """Formats user-facing IM replies from workflow payloads."""

    def combine_plan_replies(self, reply_parts: list[str]) -> str | None:
        cleaned = [part.strip() for part in reply_parts if str(part).strip()]
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return cleaned[0]
        return "\n\n".join(cleaned)

    def format_next_action_block(self, bundle: NextActionBundle) -> str:
        if not bundle.recommendations:
            return ""
        lines = ["我建议下一步可以："]
        for index, action in enumerate(bundle.recommendations[:3], start=1):
            lines.append(f"{index}. {action.title}")
            if action.reason:
                lines.append(f"   原因：{action.reason}")
            if action.command:
                lines.append(f"   你可以回复：{action.command}")
        return "\n".join(lines)

    def append_next_actions(self, reply: str | None, bundle: NextActionBundle) -> str | None:
        block = self.format_next_action_block(bundle)
        if not block:
            return reply
        cleaned_reply = str(reply or "").strip()
        return f"{cleaned_reply}\n\n{block}" if cleaned_reply else block

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

    def format_task_status_update_reply(self, task: Any, status: str) -> str:
        status_label = {
            "done": "已完成",
            "cancelled": "已取消",
            "canceled": "已取消",
        }.get(str(status or "").strip().lower(), str(status or "").strip() or "已更新")
        owner = str(getattr(task, "owner", "") or "TBD").strip()
        title = str(getattr(task, "title", "") or "未命名任务").strip()
        lines = ["【任务状态已更新】", f"- {owner} - {title}：{status_label}"]
        return "\n".join(lines)

    def format_doc_reply(self, package: dict, sync_lines: list[str]) -> str:
        sections = package.get("sections") if isinstance(package.get("sections"), list) else []
        title = str(package.get("title") or "协同文档").strip()
        mode = self._doc_sync_mode(sync_lines)
        action_lines = self._doc_action_lines(package)
        if not action_lines:
            action_lines = self._doc_inferred_action_lines(sync_lines)
        result_lines = self._doc_result_lines(sync_lines)

        lines = ["【文档同步】", f"文档：{title}"]
        if action_lines:
            lines.append("本轮任务：")
            lines.extend(action_lines)
        lines.append("执行结果：")
        lines.extend(result_lines or ["- 已完成处理。"])
        if sections and mode == "created":
            lines.append("内容预览：")
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
        return "\n".join(lines)

    def _doc_sync_mode(self, sync_lines: list[str]) -> str:
        joined = "\n".join(str(line or "") for line in sync_lines)
        if "Updated document" in joined:
            return "updated"
        if "Created document" in joined:
            return "created"
        if "No content changes detected" in joined:
            return "noop"
        if "Document sync failed" in joined or "sync failed" in joined.lower():
            return "failed"
        return "unknown"

    def _doc_action_lines(self, package: dict) -> list[str]:
        edit_plan = package.get("artifact_edit_plan") if isinstance(package.get("artifact_edit_plan"), dict) else {}
        operations = edit_plan.get("operations") or edit_plan.get("ops") if isinstance(edit_plan, dict) else []
        if not isinstance(operations, list):
            operations = []
        result: list[str] = []
        for operation in operations[:5]:
            if not isinstance(operation, dict):
                continue
            op_type = str(operation.get("type") or operation.get("op") or operation.get("action") or "").strip().lower()
            target = operation.get("target") if isinstance(operation.get("target"), dict) else {}
            payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
            target_text = self._doc_operation_target_label(op_type, target, payload)
            label = {
                "delete": "删除",
                "remove": "删除",
                "append": "新增",
                "add": "新增",
                "rename": "重命名",
                "rewrite": "重写",
                "update": "更新",
                "format": "整理格式",
                "reorder": "调整顺序",
                "compress": "压缩精简",
            }.get(op_type, op_type or "处理")
            result.append(f"- {label}：{target_text}" if target_text else f"- {label}文档内容")
        return result

    def _doc_inferred_action_lines(self, sync_lines: list[str]) -> list[str]:
        result: list[str] = []
        for line in sync_lines:
            text = str(line or "").strip().lstrip("-").strip()
            if text.startswith("Deleted sections:"):
                result.append(f"- 删除：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Appended new sections:"):
                result.append(f"- 新增：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Renamed sections:"):
                result.append(f"- 重命名：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Updated sections:"):
                result.append(f"- 更新：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Patched section bodies:"):
                result.append(f"- 改写正文：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Created document:"):
                result.append(f"- 创建文档：{text.split(':', 1)[1].strip()}")
        return result

    def _doc_operation_target_label(self, op_type: str, target: dict, payload: dict) -> str:
        if op_type == "rename":
            source = self._doc_target_label(target, payload)
            destination = self._doc_payload_value(
                payload,
                ("new_heading", "new_title", "to", "target_heading", "target_title"),
            ) or self._doc_payload_value(target, ("new_heading", "new_title", "new", "to"))
            if source and destination:
                return f"{source} -> {destination}"
            return source or destination
        if op_type in {"append", "add"}:
            return self._doc_payload_value(
                payload,
                ("heading", "title", "target_heading", "target_title", "new_heading", "new_title"),
            ) or self._doc_target_label(target, payload)
        semantic_label = self._doc_semantic_target_label(target, payload)
        if semantic_label:
            return semantic_label
        if target.get("scope") == "all":
            return "全文"
        return self._doc_target_label(target, payload)

    @staticmethod
    def _doc_payload_value(container: dict, keys: tuple[str, ...]) -> str:
        if not isinstance(container, dict):
            return ""
        for key in keys:
            value = str(container.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _doc_target_label(target: dict, payload: dict) -> str:
        values: list[str] = []
        queries = target.get("queries") if isinstance(target.get("queries"), list) else []
        values.extend(str(item).strip() for item in queries if str(item).strip())
        for container in (target, payload):
            for key in ("query", "heading", "title", "name", "label", "target", "target_heading", "target_title"):
                value = str(container.get(key) or "").strip()
                if value:
                    values.append(value)
        generic = {"doc", "document", "文档", "内容", "这一组内容", "以下内容"}
        cleaned: list[str] = []
        for value in values:
            if value.lower() in generic or value in cleaned:
                continue
            cleaned.append(value)
        return "、".join(cleaned[:3])

    def _doc_semantic_target_label(self, target: dict, payload: dict) -> str:
        scope = str(target.get("scope") or payload.get("scope") or "").strip().lower()
        if scope not in {"after", "before", "between", "body", "section", "group"}:
            return ""
        anchor = (
            str(target.get("anchor") or "").strip()
            or self._doc_target_label(target, payload).split("、", 1)[0].strip()
        )
        stop_at = str(target.get("stop_at") or payload.get("stop_at") or "").strip()
        if not anchor:
            return ""
        if scope == "after":
            return f"{anchor} 之后"
        if scope == "before":
            return f"{anchor} 之前"
        if scope == "between" and stop_at:
            return f"{anchor} 到 {stop_at} 之间"
        if scope == "body":
            return f"{anchor} 正文"
        if scope == "group":
            return f"{anchor} 这一组"
        return anchor

    def _doc_result_lines(self, sync_lines: list[str]) -> list[str]:
        translated: list[str] = []
        for line in sync_lines:
            text = str(line or "").strip().lstrip("-").strip()
            if not text:
                continue
            if text.startswith("Updated document:"):
                translated.append(f"- 已更新飞书文档：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Created document:"):
                translated.append(f"- 已创建飞书文档：{text.split(':', 1)[1].strip()}")
            elif text.startswith("No content changes detected. Keep current document:"):
                translated.append(f"- 未检测到可写入变化，当前文档保持不变：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Deleted sections:"):
                translated.append(f"- 已删除：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Updated sections:"):
                translated.append(f"- 已更新章节：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Appended new sections:"):
                translated.append(f"- 已新增章节：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Renamed sections:"):
                translated.append(f"- 已重命名：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Patched section bodies:"):
                translated.append(f"- 已改写正文：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Delete range not matched:"):
                translated.append(
                    f"- 没有匹配到要删除的范围：{text.split(':', 1)[1].strip()}。请确认标题是否仍在文档中，或换一个更准确的章节名/范围。"
                )
            elif text.startswith("Operation targets not matched:"):
                translated.append(
                    f"- 没有匹配到要操作的目标：{text.split(':', 1)[1].strip()}。本轮未对这些目标做写入，请确认标题/范围后再试。"
                )
            elif text.startswith("Requested sections are already up to date:"):
                translated.append(f"- 请求的章节已是最新：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Replaced blocks:") or text.startswith("Inserted blocks:"):
                continue
            elif text.startswith("Update strategy:"):
                continue
            elif text.startswith("Current version:"):
                translated.append(f"- 当前版本：{text.split(':', 1)[1].strip()}")
            elif text.startswith("Document URL:"):
                translated.append(f"- 链接：{text.split(':', 1)[1].strip()}")
            elif "Document sync failed" in text:
                translated.append(f"- 同步失败：{text.split(':', 1)[1].strip() if ':' in text else text}")
            elif "disabled" in text.lower():
                translated.append(f"- 同步未执行：{text}")
        if any(line.startswith("- 未检测到可写入变化") for line in translated):
            translated.append("- 可能原因：目标内容没有匹配到、已被处理过，或本轮生成内容与当前快照一致。")
        return translated

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

    def format_status_reply(self, query: str, tasks: list[Any], payload: dict) -> str:
        query_text = str(query or "")
        if any(marker in query_text for marker in ("风险", "卡点", "阻塞")):
            risks = payload.get("risks") if isinstance(payload.get("risks"), list) else []
            if not risks:
                risks = ["当前没有额外记录到新的风险项。"]
            lines = ["【当前风险】"]
            for idx, risk in enumerate(risks, start=1):
                lines.append(f"{idx}. {risk}")
            return "\n".join(lines)

        if any(marker in query_text for marker in ("总结", "结论", "摘要", "概览")):
            summary = str(payload.get("summary") or "").strip()
            if not summary:
                return "我这边还没有现成的讨论总结。你可以先让我总结一下当前讨论。"
            return "\n".join(["【当前总结】", summary])

        if not tasks:
            return "我这边还没有可用的任务快照。你可以先让我总结任务并写入文档，或补充谁负责什么任务。"

        matched = self._matching_tasks_for_query(query_text, tasks)
        if matched and any(marker in query_text for marker in ("谁负责", "谁在负责", "负责人")):
            lines = ["【任务负责人】"]
            for task in matched:
                lines.append(f"- {self._task_title(task)}：{self._task_owner(task)}")
            return "\n".join(lines)

        if any(marker in query_text for marker in ("没负责人", "没有负责人", "未分配", "待分配")):
            pending = [task for task in tasks if self._task_owner(task) in {"", "TBD", "待定"}]
            if not pending:
                return "【负责人检查】\n当前任务都已经有明确负责人，没有未分配项。"
            lines = ["【负责人检查】", "以下任务还没有明确负责人："]
            for idx, task in enumerate(pending, start=1):
                lines.append(f"{idx}. {self._task_title(task)} | 截止：{self._task_due_date(task)}")
            return "\n".join(lines)

        if any(marker in query_text for marker in ("谁负责", "谁在负责", "负责人", "分工")):
            lines = ["【当前分工】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {self._task_title(task)} -> {self._task_owner(task)}")
            return "\n".join(lines)

        if any(marker in query_text for marker in ("截止", "到期", "时间")):
            lines = ["【时间节点】"]
            for idx, task in enumerate(tasks, start=1):
                lines.append(f"{idx}. {self._task_title(task)} | 截止：{self._task_due_date(task)}")
            return "\n".join(lines)

        completed = sum(1 for task in tasks if str(self._task_status(task)).lower() == "done")
        unassigned = sum(1 for task in tasks if self._task_owner(task) in {"", "TBD", "待定"})
        lines = [
            "【当前任务】",
            f"- 任务总数：{len(tasks)}",
            f"- 已完成：{completed}",
            f"- 待确认负责人：{unassigned}",
            "任务明细：",
        ]
        for idx, task in enumerate(tasks[:10], start=1):
            lines.append(
                f"{idx}. {self._task_title(task)} | 负责人：{self._task_owner(task)} | 截止：{self._task_due_date(task)} | 状态：{self._task_status(task)}"
            )
        if len(tasks) > 10:
            lines.append(f"... 还有 {len(tasks) - 10} 个任务未展开。")
        return "\n".join(lines)

    def _matching_tasks_for_query(self, query: str, tasks: list[Any]) -> list[Any]:
        query_text = str(query or "").strip().lower()
        if not query_text:
            return []
        matched: list[Any] = []
        for task in tasks:
            title = self._task_title(task).lower()
            owner = self._task_owner(task).lower()
            if title and (title in query_text or query_text in title):
                matched.append(task)
                continue
            if owner and owner not in {"tbd", "待定"} and owner in query_text:
                matched.append(task)
        return matched

    @staticmethod
    def _task_field(task: Any, name: str, default: str = "") -> str:
        if isinstance(task, dict):
            return str(task.get(name) or default).strip()
        return str(getattr(task, name, default) or default).strip()

    @classmethod
    def _task_title(cls, task: Any) -> str:
        return cls._task_field(task, "title", "未命名任务")

    @classmethod
    def _task_owner(cls, task: Any) -> str:
        return cls._task_field(task, "owner", "TBD") or "TBD"

    @classmethod
    def _task_due_date(cls, task: Any) -> str:
        return cls._task_field(task, "due_date", "TBD") or "TBD"

    @classmethod
    def _task_status(cls, task: Any) -> str:
        return cls._task_field(task, "status", "draft") or "draft"

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
