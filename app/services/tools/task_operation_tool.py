from __future__ import annotations

from app.schemas.task import TaskItem
from app.services.text_analysis import apply_discussion_updates, extract_tasks, normalize_tasks


class TaskOperationTool:
    """Pure helpers for merging task snapshots and LLM task operations."""

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
    def update_current_tasks_from_discussion(
        current_tasks: list[TaskItem],
        source_text: str,
        llm_tasks: list[TaskItem],
    ) -> list[TaskItem]:
        updated = apply_discussion_updates(current_tasks, source_text)
        explicit_tasks = normalize_tasks(extract_tasks(source_text))
        if explicit_tasks:
            return TaskOperationTool.merge_task_items(updated, explicit_tasks)
        if llm_tasks:
            return TaskOperationTool.merge_task_items(updated, llm_tasks)
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


def _normalized(value: str | None) -> str:
    return " ".join((value or "").lower().split())
