from __future__ import annotations

from app.schemas.task import TaskItem
from app.services.text_analysis import apply_discussion_updates, extract_tasks, normalize_tasks


class TaskOperationTool:
    """Pure helpers for merging task snapshots and LLM task operations."""

    DONE_MARKERS = (
        "已经完成",
        "已完成",
        "完成了",
        "做完了",
        "搞定了",
        "弄完了",
        "done",
        "completed",
    )
    CANCEL_MARKERS = (
        "不用做了",
        "先不做",
        "不做了",
        "取消",
        "cancelled",
        "canceled",
    )
    TITLE_HINT_TOKENS = (
        "后端",
        "前端",
        "接口",
        "联调",
        "测试",
        "文档",
        "材料",
        "汇报",
        "ppt",
        "画布",
        "流程图",
        "架构",
        "产品",
        "设计",
        "部署",
        "验收",
        "风险",
        "路演",
        "demo",
    )
    DIRECT_ASSIGNMENT_MARKERS = (
        "需要有人",
        "找个人",
        "找人",
        "谁来",
        "谁能",
        "帮我完成",
        "我来",
        "我负责",
        "我去",
        "我做",
        "我处理",
        "我推进",
    )

    @staticmethod
    def apply_llm_operations(current_tasks: list[TaskItem], operations: list[dict]) -> list[TaskItem]:
        refreshed = [task.model_copy(deep=True) for task in current_tasks]
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "").strip().lower()
            match_hint = operation.get("match_hint") if isinstance(operation.get("match_hint"), dict) else {}
            task_payload = operation.get("task") if isinstance(operation.get("task"), dict) else None

            if action == "create" and task_payload:
                refreshed.append(TaskItem.model_validate(task_payload))
                continue

            target_index = TaskOperationTool.find_operation_target(refreshed, match_hint, task_payload)
            if target_index is None:
                if action == "create" and task_payload:
                    refreshed.append(TaskItem.model_validate(task_payload))
                continue

            if action == "remove":
                refreshed.pop(target_index)
                continue

            if action == "update" and task_payload:
                refreshed[target_index] = TaskItem.model_validate(task_payload)

        return refreshed

    @staticmethod
    def merge_task_items(current_tasks: list[TaskItem], refreshed_tasks: list[TaskItem]) -> list[TaskItem]:
        if not current_tasks:
            return [task.model_copy(deep=True) for task in refreshed_tasks]

        merged = [task.model_copy(deep=True) for task in current_tasks]
        for task in refreshed_tasks:
            target_index = TaskOperationTool.find_merge_target(merged, task)
            if target_index is None:
                merged.append(task.model_copy(deep=True))
                continue
            merged[target_index] = task.model_copy(deep=True)
        return merged

    @staticmethod
    def merge_assignment_items(current_tasks: list[TaskItem], incoming_tasks: list[TaskItem]) -> list[TaskItem]:
        merged = [task.model_copy(deep=True) for task in current_tasks]
        for incoming in incoming_tasks:
            incoming_owner = _normalized(incoming.owner)
            incoming_title = _normalized(incoming.title)
            if incoming_title and incoming_owner not in {"", "tbd"}:
                tbd_matches = [
                    idx
                    for idx, task in enumerate(merged)
                    if _normalized(task.title) == incoming_title
                    and _normalized(task.owner) in {"", "tbd"}
                    and _normalized(task.status) not in {"done", "cancelled", "canceled"}
                ]
                if len(tbd_matches) == 1:
                    merged[tbd_matches[0]] = incoming.model_copy(deep=True)
                    continue

            target_index = TaskOperationTool.find_merge_target(merged, incoming)
            if target_index is None:
                merged.append(incoming.model_copy(deep=True))
                continue
            merged[target_index] = incoming.model_copy(deep=True)
        return merged

    @staticmethod
    def update_current_tasks_from_discussion(
        current_tasks: list[TaskItem],
        source_text: str,
        llm_tasks: list[TaskItem],
        *,
        actor_names: list[str] | None = None,
    ) -> list[TaskItem]:
        status_update = TaskOperationTool.resolve_status_update(current_tasks, source_text, actor_names=actor_names)
        if status_update.get("updated"):
            return status_update["tasks"]
        if status_update.get("detected"):
            return [task.model_copy(deep=True) for task in current_tasks]

        updated = apply_discussion_updates(current_tasks, source_text)
        explicit_tasks = normalize_tasks(extract_tasks(source_text))
        if explicit_tasks:
            return TaskOperationTool.merge_assignment_items(updated, explicit_tasks)
        if llm_tasks:
            return TaskOperationTool.merge_assignment_items(updated, llm_tasks)
        return updated

    @staticmethod
    def find_merge_target(tasks: list[TaskItem], incoming_task: TaskItem) -> int | None:
        incoming_title = _normalized(incoming_task.title)
        incoming_owner = _normalized(incoming_task.owner)

        exact_matches = [
            idx
            for idx, task in enumerate(tasks)
            if _normalized(task.title) == incoming_title
            and _normalized(task.owner) == incoming_owner
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]

        owner_missing = incoming_owner in {"", "tbd"}
        if owner_missing:
            title_matches = [
                idx
                for idx, task in enumerate(tasks)
                if _normalized(task.title) == incoming_title
            ]
            if len(title_matches) == 1:
                return title_matches[0]

        return None

    @staticmethod
    def find_operation_target(
        tasks: list[TaskItem],
        match_hint: dict,
        task_payload: dict | None,
    ) -> int | None:
        hint_title = str(match_hint.get("title") or "").strip()
        hint_owner = str(match_hint.get("owner") or "").strip()
        payload_title = str(task_payload.get("title") or "").strip() if task_payload else ""
        payload_owner = str(task_payload.get("owner") or "").strip() if task_payload else ""

        candidates: list[tuple[str, str]] = []
        if hint_title or hint_owner:
            candidates.append((hint_title, hint_owner))
        if payload_title or payload_owner:
            candidates.append((payload_title, payload_owner))

        for title, owner in candidates:
            exact_matches = [
                idx
                for idx, task in enumerate(tasks)
                if (not title or _normalized(task.title) == _normalized(title))
                and (not owner or _normalized(task.owner) == _normalized(owner))
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]

        for title, _ in candidates:
            if not title:
                continue
            title_matches = [idx for idx, task in enumerate(tasks) if _normalized(task.title) == _normalized(title)]
            if len(title_matches) == 1:
                return title_matches[0]

        return None

    @staticmethod
    def has_status_update_signal(text: str) -> bool:
        return TaskOperationTool.status_from_text(text) is not None

    @staticmethod
    def status_from_text(text: str) -> str | None:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return None
        if any(marker in lowered for marker in TaskOperationTool.CANCEL_MARKERS):
            return "cancelled"
        if any(marker in lowered for marker in TaskOperationTool.DONE_MARKERS):
            return "done"
        return None

    @staticmethod
    def resolve_status_update(
        current_tasks: list[TaskItem],
        source_text: str,
        *,
        actor_names: list[str] | None = None,
    ) -> dict:
        tasks = [task.model_copy(deep=True) for task in current_tasks]
        update_lines = [
            line.strip()
            for line in str(source_text or "").splitlines()
            if TaskOperationTool.has_status_update_signal(line)
        ]
        if not update_lines:
            return {"detected": False, "updated": False, "tasks": tasks}

        for line in update_lines:
            status = TaskOperationTool.status_from_text(line)
            if not status:
                continue
            target_index, candidates = TaskOperationTool.find_status_update_target(
                tasks,
                line,
                actor_names=actor_names,
            )
            if target_index is None:
                return {
                    "detected": True,
                    "updated": False,
                    "tasks": tasks,
                    "status": status,
                    "clarification": TaskOperationTool.status_update_clarification(line, candidates, status=status),
                }
            target = tasks[target_index]
            note = f"状态更新：{line}"
            notes = target.notes or ""
            if note not in notes:
                notes = f"{notes}；{note}".strip("；")
            tasks[target_index] = target.model_copy(update={"status": status, "notes": notes})

        return {
            "detected": True,
            "updated": True,
            "tasks": tasks,
            "status": status,
            "updated_task": tasks[target_index] if update_lines else None,
        }

    @staticmethod
    def resolve_status_update_from_intent(
        current_tasks: list[TaskItem],
        intent_result: dict,
        *,
        actor_names: list[str] | None = None,
        source_text: str = "",
    ) -> dict:
        tasks = [task.model_copy(deep=True) for task in current_tasks]
        if not isinstance(intent_result, dict):
            return {"detected": False, "updated": False, "tasks": tasks}
        if intent_result.get("intent") != "task_status_update":
            return {"detected": False, "updated": False, "tasks": tasks}

        status = str(intent_result.get("status") or "").strip().lower()
        if status not in {"done", "cancelled"}:
            return {"detected": True, "updated": False, "tasks": tasks}

        active_candidates = [
            (idx, task)
            for idx, task in enumerate(tasks)
            if str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
        ]
        if not active_candidates:
            return {
                "detected": True,
                "updated": False,
                "tasks": tasks,
                "status": status,
                "clarification": TaskOperationTool.status_update_clarification(source_text, [], status=status),
            }

        actor = intent_result.get("actor") if isinstance(intent_result.get("actor"), dict) else {}
        actor_source = str(actor.get("source") or "unknown").strip().lower()
        actor_text = str(actor.get("text") or "").strip()
        scoped_candidates = active_candidates
        if actor_source == "sender":
            actor_values = {_normalized(name) for name in actor_names or [] if _normalized(name)}
            sender_matches = [
                (idx, task)
                for idx, task in active_candidates
                if _normalized(task.owner) in actor_values
            ]
            hint_matches = TaskOperationTool._filter_tasks_by_intent_hint(active_candidates, intent_result, source_text)
            if not sender_matches:
                return {
                    "detected": True,
                    "updated": False,
                    "tasks": tasks,
                    "status": status,
                    "clarification": TaskOperationTool.status_update_clarification(
                        source_text,
                        [task for _, task in hint_matches or active_candidates[:5]],
                        status=status,
                    ),
                }
            scoped_candidates = sender_matches
        elif actor_text:
            owner_matches = [
                (idx, task)
                for idx, task in active_candidates
                if _text_contains_label(actor_text, task.owner) or _text_contains_label(task.owner, actor_text)
            ]
            if owner_matches:
                scoped_candidates = owner_matches

        hint = str(intent_result.get("task_hint") or "").strip()
        title_matches = TaskOperationTool._filter_tasks_by_intent_hint(scoped_candidates, intent_result, source_text)
        if hint and not title_matches:
            return {
                "detected": True,
                "updated": False,
                "tasks": tasks,
                "status": status,
                "clarification": TaskOperationTool.status_update_clarification(
                    source_text,
                    [task for _, task in scoped_candidates[:5]],
                    status=status,
                ),
            }
        candidates = title_matches or scoped_candidates
        if len(candidates) != 1:
            return {
                "detected": True,
                "updated": False,
                "tasks": tasks,
                "status": status,
                "clarification": TaskOperationTool.status_update_clarification(
                    source_text,
                    [task for _, task in candidates[:5]],
                    status=status,
                ),
            }

        target_index, target = candidates[0]
        note = f"LLM task intent update: {source_text or intent_result.get('task_hint') or status}"
        notes = target.notes or ""
        if note not in notes:
            notes = f"{notes}; {note}".strip("; ")
        tasks[target_index] = target.model_copy(update={"status": status, "notes": notes})
        return {
            "detected": True,
            "updated": True,
            "tasks": tasks,
            "status": status,
            "updated_task": tasks[target_index],
        }

    @staticmethod
    def tasks_from_assignment_intent(
        intent_result: dict,
        *,
        actor_names: list[str] | None = None,
        source_text: str = "",
    ) -> list[TaskItem]:
        if not isinstance(intent_result, dict) or intent_result.get("intent") != "task_assignment":
            return []
        title = str(intent_result.get("task_hint") or "").strip()
        if not title:
            return []

        assignee = intent_result.get("assignee") if isinstance(intent_result.get("assignee"), dict) else {}
        assignee_source = str(assignee.get("source") or "unknown").strip().lower()
        assignee_text = str(assignee.get("text") or "").strip()
        if assignee_source == "sender":
            owner = next((name for name in actor_names or [] if str(name or "").strip()), "TBD")
        elif assignee_source in {"literal", "mentioned"} and assignee_text:
            owner = assignee_text
        else:
            owner = "TBD"

        status = str(intent_result.get("status") or "draft").strip().lower()
        if status not in {"draft", "done", "cancelled"}:
            status = "draft"
        return [
            TaskItem(
                title=title,
                owner=owner or "TBD",
                priority="medium",
                due_date="TBD",
                status=status,
                notes=str(intent_result.get("reason") or source_text or "").strip(),
            )
        ]

    @staticmethod
    def resolve_assignment_from_intent(
        current_tasks: list[TaskItem],
        intent_result: dict,
        *,
        actor_names: list[str] | None = None,
        source_text: str = "",
    ) -> dict:
        tasks = [task.model_copy(deep=True) for task in current_tasks]
        if not isinstance(intent_result, dict) or intent_result.get("intent") != "task_assignment":
            return {"detected": False, "updated": False, "tasks": tasks}

        owner = TaskOperationTool.owner_from_assignment_intent(
            intent_result,
            actor_names=actor_names,
        )
        if not owner:
            owner = "TBD"

        active_candidates = [
            (idx, task)
            for idx, task in enumerate(tasks)
            if str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
        ]
        if not active_candidates:
            return {"detected": True, "updated": False, "tasks": tasks}

        candidates = TaskOperationTool.find_assignment_targets_from_intent(
            active_candidates,
            intent_result,
            source_text=source_text,
        )
        if len(candidates) == 1:
            target_index, target = candidates[0]
            owner_hint = TaskOperationTool.assignment_owner_hint(intent_result, source_text)
            target_owner = _normalized(target.owner)
            new_owner = _normalized(owner)
            explicit_owner_match = bool(
                owner_hint
                and (
                    _text_contains_label(owner_hint, target.owner)
                    or _text_contains_label(f"{intent_result.get('task_hint') or ''} {source_text}", target.owner)
                )
            )
            if target_owner not in {"", "tbd"} and target_owner != new_owner and not explicit_owner_match:
                return {
                    "detected": True,
                    "updated": False,
                    "tasks": tasks,
                    "clarification": TaskOperationTool.assignment_clarification(
                        source_text,
                        [target],
                        owner=owner,
                    ),
                }
            note = f"LLM task assignment update: {source_text or intent_result.get('task_hint') or owner}"
            notes = target.notes or ""
            if note not in notes:
                notes = f"{notes}; {note}".strip("; ")
            tasks[target_index] = target.model_copy(update={"owner": owner, "notes": notes})
            return {
                "detected": True,
                "updated": True,
                "tasks": tasks,
                "updated_task": tasks[target_index],
            }
        if len(candidates) > 1:
            return {
                "detected": True,
                "updated": False,
                "tasks": tasks,
                "clarification": TaskOperationTool.assignment_clarification(
                    source_text,
                    [task for _, task in candidates[:5]],
                    owner=owner,
                ),
            }

        hint = str(intent_result.get("task_hint") or "").strip()
        if hint and current_tasks:
            likely = TaskOperationTool.find_loose_assignment_candidates(
                active_candidates,
                intent_result,
                source_text=source_text,
            )
            return {
                "detected": True,
                "updated": False,
                "tasks": tasks,
                "clarification": TaskOperationTool.assignment_clarification(
                    source_text,
                    [task for _, task in likely[:5]],
                    owner=owner,
                ),
            }
        return {"detected": True, "updated": False, "tasks": tasks}

    @staticmethod
    def owner_from_assignment_intent(
        intent_result: dict,
        *,
        actor_names: list[str] | None = None,
    ) -> str:
        assignee = intent_result.get("assignee") if isinstance(intent_result.get("assignee"), dict) else {}
        assignee_source = str(assignee.get("source") or "unknown").strip().lower()
        assignee_text = str(assignee.get("text") or "").strip()
        if assignee_source == "sender":
            return next((name for name in actor_names or [] if str(name or "").strip()), "TBD")
        if assignee_source in {"literal", "mentioned"} and assignee_text:
            return assignee_text
        if assignee_source == "tbd":
            return "TBD"
        return ""

    @staticmethod
    def find_assignment_targets_from_intent(
        candidates: list[tuple[int, TaskItem]],
        intent_result: dict,
        *,
        source_text: str,
    ) -> list[tuple[int, TaskItem]]:
        hint = str(intent_result.get("task_hint") or "").strip()
        owner_hint = TaskOperationTool.assignment_owner_hint(intent_result, source_text)
        probe = f"{hint} {source_text}".strip()
        matched = [
            (idx, task)
            for idx, task in candidates
            if (
                TaskOperationTool.line_matches_task_title(probe, task.title)
                or TaskOperationTool._has_compact_overlap(hint, task.title)
            )
            and (
                not owner_hint
                or _text_contains_label(owner_hint, task.owner)
                or _text_contains_label(probe, task.owner)
            )
        ]
        if matched:
            return matched
        return [
            (idx, task)
            for idx, task in candidates
            if TaskOperationTool.line_matches_task_title(probe, task.title)
            or TaskOperationTool._has_compact_overlap(hint, task.title)
        ]

    @staticmethod
    def find_loose_assignment_candidates(
        candidates: list[tuple[int, TaskItem]],
        intent_result: dict,
        *,
        source_text: str,
    ) -> list[tuple[int, TaskItem]]:
        hint = str(intent_result.get("task_hint") or "").strip()
        probe = f"{hint} {source_text}".strip()
        owner_hint = TaskOperationTool.assignment_owner_hint(intent_result, source_text)
        owner_matches = [
            (idx, task)
            for idx, task in candidates
            if owner_hint
            and (
                _text_contains_label(owner_hint, task.owner)
                or _text_contains_label(probe, task.owner)
            )
        ]
        if owner_matches:
            return owner_matches
        return candidates[:5]

    @staticmethod
    def assignment_owner_hint(intent_result: dict, source_text: str) -> str:
        target = intent_result.get("target_task") if isinstance(intent_result.get("target_task"), dict) else {}
        for key in ("owner", "owner_hint", "current_owner", "current_owner_hint"):
            value = str(target.get(key) or "").strip()
            if value:
                return value
        actor = intent_result.get("actor") if isinstance(intent_result.get("actor"), dict) else {}
        actor_source = str(actor.get("source") or "").strip().lower()
        actor_text = str(actor.get("text") or "").strip()
        if actor_source in {"literal", "mentioned"} and actor_text:
            return actor_text
        assignee = intent_result.get("assignee") if isinstance(intent_result.get("assignee"), dict) else {}
        if str(assignee.get("source") or "").strip().lower() == "sender":
            return TaskOperationTool._owner_hint_before_sender_claim(source_text)
        return ""

    @staticmethod
    def _owner_hint_before_sender_claim(source_text: str) -> str:
        text = str(source_text or "").strip()
        for marker in ("由我", "交给我", "我来", "我负责", "我去", "我做", "我处理"):
            index = text.find(marker)
            if index > 0:
                prefix = text[:index].strip()
                if len(prefix) >= 2:
                    return prefix[-4:]
        return ""

    @staticmethod
    def assignment_clarification(line: str, candidates: list[TaskItem], *, owner: str) -> dict:
        options = [TaskOperationTool.task_option_label(task) for task in candidates[:5]]
        reason = "这句话像是在调整任务负责人，但我无法唯一确定要更新哪一项任务。"
        if not options:
            reason = "这句话像是在认领或分配任务，但当前任务列表里没有可安全匹配的任务。"
        return {
            "question": f"你说的“{line}”具体要把哪一项任务改为 {owner or 'TBD'} 负责？",
            "reason": reason,
            "options": options or ["先查看任务列表", "重新说明任务标题和原负责人"],
            "candidates": [task.model_dump() for task in candidates[:5]],
            "target_status": "draft",
            "blocking": True,
        }

    @staticmethod
    def _filter_tasks_by_intent_hint(
        candidates: list[tuple[int, TaskItem]],
        intent_result: dict,
        source_text: str,
    ) -> list[tuple[int, TaskItem]]:
        hint = str(intent_result.get("task_hint") or "").strip()
        if not hint:
            return []
        probe = f"{hint} {source_text}".strip()
        return [
            (idx, task)
            for idx, task in candidates
            if TaskOperationTool.line_matches_task_title(probe, task.title)
            or TaskOperationTool._has_compact_overlap(hint, task.title)
        ]

    @staticmethod
    def _has_compact_overlap(left: str, right: str) -> bool:
        left_compact = _normalized(left).replace(" ", "")
        right_compact = _normalized(right).replace(" ", "")
        if not left_compact or not right_compact:
            return False
        if _is_ascii_wordish(left_compact) and _is_ascii_wordish(right_compact):
            if left_compact == right_compact:
                return True
            left_tokens = _word_tokens(left)
            right_tokens = _word_tokens(right)
            if any(left_token == right_token and len(left_token) >= 2 for left_token in left_tokens for right_token in right_tokens):
                return True
            return any(
                len(left_token) >= 4
                and len(right_token) >= 4
                and (left_token in right_token or right_token in left_token)
                for left_token in left_tokens
                for right_token in right_tokens
            )
        if min(len(left_compact), len(right_compact)) < 2:
            return False
        if left_compact in right_compact or right_compact in left_compact:
            return True
        if len(left_compact) < 2 or len(right_compact) < 2:
            return False
        window = 3 if len(left_compact) >= 3 and len(right_compact) >= 3 else 2
        return any(left_compact[index : index + window] in right_compact for index in range(len(left_compact) - window + 1))

    @staticmethod
    def find_status_update_target(
        tasks: list[TaskItem],
        line: str,
        *,
        actor_names: list[str] | None = None,
    ) -> tuple[int | None, list[TaskItem]]:
        active_candidates = [
            (idx, task)
            for idx, task in enumerate(tasks)
            if str(task.status or "").strip().lower() not in {"done", "cancelled", "canceled"}
        ]
        if not active_candidates:
            return None, []

        owner_matches = [
            (idx, task)
            for idx, task in active_candidates
            if task.owner and _normalized(task.owner) != "tbd" and _text_contains_label(line, task.owner)
        ]
        actor_matches: list[tuple[int, TaskItem]] = []
        first_person = TaskOperationTool.has_first_person_reference(line)
        if not owner_matches and first_person:
            actor_values = {_normalized(name) for name in actor_names or [] if _normalized(name)}
            actor_matches = [
                (idx, task)
                for idx, task in active_candidates
                if _normalized(task.owner) in actor_values
            ]

            title_candidates = [
                (idx, task)
                for idx, task in active_candidates
                if TaskOperationTool.line_matches_task_title(line, task.title)
            ]
            if not actor_matches:
                return None, [task for _, task in title_candidates or active_candidates[:5]]
            actor_title_matches = [
                (idx, task)
                for idx, task in actor_matches
                if TaskOperationTool.line_matches_task_title(line, task.title)
            ]
            if len(actor_title_matches) == 1:
                return actor_title_matches[0][0], [actor_title_matches[0][1]]
            if actor_title_matches:
                return None, [task for _, task in actor_title_matches]
            if title_candidates or TaskOperationTool.line_has_title_hint(line):
                return None, [task for _, task in actor_matches]

        scoped_candidates = owner_matches or actor_matches or active_candidates
        title_matches = [
            (idx, task)
            for idx, task in scoped_candidates
            if TaskOperationTool.line_matches_task_title(line, task.title)
        ]
        if not owner_matches and not actor_matches and _has_conflicting_ascii_actor_prefix(
            line,
            [task for _, task in title_matches],
        ):
            return None, [task for _, task in title_matches]

        if len(title_matches) == 1:
            return title_matches[0][0], [task for _, task in title_matches]
        if len(title_matches) > 1:
            return None, [task for _, task in title_matches]
        if actor_matches and len(actor_matches) == 1:
            return actor_matches[0][0], [actor_matches[0][1]]
        if actor_matches:
            return None, [task for _, task in actor_matches]
        if owner_matches and len(owner_matches) == 1:
            return owner_matches[0][0], [owner_matches[0][1]]
        if owner_matches:
            return None, [task for _, task in owner_matches]
        return None, [task for _, task in scoped_candidates[:5]]

    @staticmethod
    def line_matches_task_title(line: str, title: str) -> bool:
        normalized_line = _normalized(line).replace(" ", "")
        normalized_title = _normalized(title).replace(" ", "")
        if normalized_title:
            if _is_ascii_wordish(normalized_title):
                line_tokens = _word_tokens(line)
                title_tokens = _word_tokens(title)
                if title_tokens and _tokens_contain_sequence(line_tokens, title_tokens):
                    return True
            elif len(normalized_title) >= 2 and normalized_title in normalized_line:
                return True
        lowered_line = str(line or "").lower()
        lowered_title = str(title or "").lower()
        return any(token in lowered_title and token in lowered_line for token in TaskOperationTool.TITLE_HINT_TOKENS)

    @staticmethod
    def line_has_title_hint(line: str) -> bool:
        lowered_line = str(line or "").lower()
        return any(token in lowered_line for token in TaskOperationTool.TITLE_HINT_TOKENS)

    @staticmethod
    def status_update_clarification(line: str, candidates: list[TaskItem], *, status: str | None = None) -> dict:
        options = [TaskOperationTool.task_option_label(task) for task in candidates[:5]]
        reason = "这句话是在更新任务状态，但缺少足够的任务标题信息。"
        if not options:
            reason = "这句话是在更新任务状态，但当前任务列表里没有可匹配的未完成任务。"
        return {
            "question": f"你说的“{line}”具体是指哪一项任务？",
            "reason": reason,
            "options": options or ["先查看任务列表", "重新说明任务标题和负责人"],
            "candidates": [task.model_dump() for task in candidates[:5]],
            "target_status": status or TaskOperationTool.status_from_text(line) or "done",
            "blocking": True,
        }

    @staticmethod
    def task_option_label(task: TaskItem) -> str:
        owner = task.owner or "TBD"
        return f"{owner} - {task.title}"

    @staticmethod
    def has_first_person_reference(text: str) -> bool:
        value = str(text or "").strip()
        return bool(value) and (
            value.startswith("我")
            or value.startswith("我的")
            or any(marker in value for marker in (" 我已", " 我来", " 我负责", " 我完成"))
        )

    @staticmethod
    def normalize_first_person_task_text(text: str, actor_name: str | None) -> str:
        actor = str(actor_name or "").strip()
        value = str(text or "")
        if not actor:
            return value
        value = value.strip()
        if not value:
            return value
        replacements = (
            ("我已经", f"{actor}已经"),
            ("我已", f"{actor}已"),
            ("我来", f"{actor}来"),
            ("我负责", f"{actor}负责"),
            ("我去", f"{actor}去"),
            ("我做", f"{actor}做"),
            ("我处理", f"{actor}处理"),
            ("我推进", f"{actor}推进"),
            ("我完成", f"{actor}完成"),
        )
        for source, replacement in replacements:
            if value.startswith(source):
                return replacement + value[len(source):]
        if value.startswith("我的"):
            return actor + value[1:]
        return value

    @staticmethod
    def has_direct_assignment_signal(text: str) -> bool:
        lowered = str(text or "").strip().lower()
        return any(marker in lowered for marker in TaskOperationTool.DIRECT_ASSIGNMENT_MARKERS)

    @staticmethod
    def task_assignment_clarification(current_tasks: list[TaskItem], incoming_tasks: list[TaskItem]) -> dict | None:
        for incoming in incoming_tasks:
            incoming_owner = _normalized(incoming.owner)
            incoming_title = _normalized(incoming.title)
            if not incoming_title:
                continue
            title_matches = [
                task
                for task in current_tasks
                if _normalized(task.title) == incoming_title
                and _normalized(task.status) not in {"done", "cancelled", "canceled"}
            ]
            assigned_matches = [task for task in title_matches if _normalized(task.owner) not in {"", "tbd"}]
            if incoming_owner in {"", "tbd"} and assigned_matches:
                return {
                    "question": f"“{incoming.title}”现在已有负责人，你是想新增协助人还是调整负责人？",
                    "reason": "这句话像是在新增求助任务，但当前任务列表里已有同名任务，直接覆盖会丢失原负责人。",
                    "options": [
                        f"保留现有负责人：{TaskOperationTool.task_option_label(assigned_matches[0])}",
                        f"新增待确认协助任务：TBD - {incoming.title}",
                        f"改为重新确认负责人：{incoming.title}",
                    ],
                    "blocking": True,
                }
            conflicting_matches = [
                task
                for task in assigned_matches
                if incoming_owner not in {"", "tbd"} and _normalized(task.owner) != incoming_owner
            ]
            if conflicting_matches:
                return {
                    "question": f"“{incoming.title}”现在已有负责人，你是想调整负责人还是新增协助人？",
                    "reason": "这句话像是在认领或分配任务，但同名未完成任务已有其他负责人，直接合并会造成重复或覆盖。",
                    "options": [
                        f"保留现有负责人：{TaskOperationTool.task_option_label(conflicting_matches[0])}",
                        f"改为新负责人：{incoming.owner} - {incoming.title}",
                        f"新增协助任务：{incoming.owner} - {incoming.title}",
                    ],
                    "blocking": True,
                }
        return None


def _normalized(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _is_ascii_wordish(value: str) -> bool:
    return bool(value) and all(ord(char) < 128 for char in value)


def _word_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in str(value or "").lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _tokens_contain_sequence(tokens: list[str], sequence: list[str]) -> bool:
    if not tokens or not sequence or len(sequence) > len(tokens):
        return False
    for index in range(len(tokens) - len(sequence) + 1):
        if tokens[index : index + len(sequence)] == sequence:
            return True
    return False


def _text_contains_label(text: str | None, label: str | None) -> bool:
    normalized_text = _normalized(text)
    normalized_label = _normalized(label)
    if not normalized_text or not normalized_label:
        return False
    if _is_ascii_wordish(normalized_label.replace(" ", "")):
        return _tokens_contain_sequence(_word_tokens(text or ""), _word_tokens(label or ""))
    if len(normalized_label.replace(" ", "")) < 2:
        return normalized_text == normalized_label
    return normalized_label.replace(" ", "") in normalized_text.replace(" ", "")


def _has_conflicting_ascii_actor_prefix(line: str, candidates: list[TaskItem]) -> bool:
    if not candidates:
        return False
    tokens = _word_tokens(line)
    if len(tokens) < 2:
        return False
    status_tokens = {"done", "completed", "cancelled", "canceled"}
    status_index = next((idx for idx, token in enumerate(tokens[:5]) if token in status_tokens), None)
    if status_index is None or status_index == 0:
        return False
    actor_prefix = tokens[:status_index]
    if actor_prefix[0] in {"i", "we", "my", "our", "the", "task"}:
        return False
    candidate_owners = [_word_tokens(task.owner) for task in candidates if _word_tokens(task.owner)]
    if not candidate_owners:
        return False
    if any(_tokens_contain_sequence(actor_prefix, owner_tokens) for owner_tokens in candidate_owners):
        return False
    candidate_title_tokens = [_word_tokens(task.title) for task in candidates if _word_tokens(task.title)]
    if any(_tokens_contain_sequence(actor_prefix, title_tokens) for title_tokens in candidate_title_tokens):
        return False
    return True
