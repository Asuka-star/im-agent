from app.schemas.analyze import AgentTrace, AnalyzeRequest
from app.schemas.task import TaskItem


class PlannerAgent:
    def run(self, payload: AnalyzeRequest) -> tuple[list[TaskItem], AgentTrace]:
        task = TaskItem(
            title="Review incoming discussion and extract actions",
            owner="TBD",
            priority="medium",
            due_date="TBD",
            status="draft",
            notes=payload.raw_text[:120],
        )
        trace = AgentTrace(
            agent="planner",
            summary="Created an initial task draft from the incoming discussion.",
        )
        return [task], trace
