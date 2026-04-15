from app.schemas.analyze import AgentTrace
from app.schemas.task import TaskItem
from app.services.text_analysis import infer_risks


class ReviewerAgent:
    def run(self, tasks: list[TaskItem]) -> tuple[list[str], AgentTrace]:
        risks = infer_risks(tasks)
        trace = AgentTrace(
            agent="reviewer",
            summary=f"Reviewed {len(tasks)} task(s) and generated {len(risks)} risk signal(s).",
        )
        return risks, trace
