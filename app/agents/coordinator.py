from app.schemas.analyze import AgentTrace
from app.schemas.task import TaskItem


class CoordinatorAgent:
    def run(self, tasks: list[TaskItem]) -> tuple[list[TaskItem], AgentTrace]:
        updated_tasks: list[TaskItem] = []
        for task in tasks:
            updated_tasks.append(
                task.model_copy(
                    update={
                        "owner": "Project Owner",
                        "due_date": "This week",
                    }
                )
            )

        trace = AgentTrace(
            agent="coordinator",
            summary="Added a tentative owner and due date to each task.",
        )
        return updated_tasks, trace
