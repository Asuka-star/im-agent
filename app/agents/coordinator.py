from app.schemas.analyze import AgentTrace
from app.schemas.task import TaskItem
from app.services.due_date import normalize_task_dates
from app.services.text_analysis import normalize_tasks


class CoordinatorAgent:
    def run(self, tasks: list[TaskItem]) -> tuple[list[TaskItem], AgentTrace]:
        updated_tasks = normalize_task_dates(normalize_tasks(tasks))
        missing_owner_count = sum(1 for task in updated_tasks if task.owner == "TBD")
        missing_due_count = sum(1 for task in updated_tasks if task.due_date == "TBD")
        trace = AgentTrace(
            agent="coordinator",
            summary=(
                f"Normalized {len(updated_tasks)} task(s); "
                f"{missing_owner_count} still need owners and {missing_due_count} still need due dates."
            ),
        )
        return updated_tasks, trace
