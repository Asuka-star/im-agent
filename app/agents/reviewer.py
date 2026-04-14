from app.schemas.analyze import AgentTrace
from app.schemas.task import TaskItem


class ReviewerAgent:
    def run(self, tasks: list[TaskItem]) -> tuple[list[str], AgentTrace]:
        risks: list[str] = []
        for task in tasks:
            if task.owner == "TBD":
                risks.append(f"Task '{task.title}' has no confirmed owner.")
            if task.due_date == "TBD":
                risks.append(f"Task '{task.title}' has no confirmed due date.")

        if not risks:
            risks.append("Task ownership and due date are still tentative and need confirmation.")

        trace = AgentTrace(
            agent="reviewer",
            summary="Reviewed the drafted tasks and highlighted coordination risks.",
        )
        return risks, trace
