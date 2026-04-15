from app.schemas.analyze import AgentTrace, AnalyzeRequest
from app.schemas.task import TaskItem
from app.services.text_analysis import extract_tasks


class PlannerAgent:
    def run(self, payload: AnalyzeRequest) -> tuple[list[TaskItem], AgentTrace]:
        tasks = extract_tasks(payload.raw_text)
        trace = AgentTrace(
            agent="planner",
            summary=f"Extracted {len(tasks)} initial task candidate(s) from the latest discussion.",
        )
        return tasks, trace
