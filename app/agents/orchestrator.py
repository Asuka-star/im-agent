from app.agents.coordinator import CoordinatorAgent
from app.agents.memory import MemoryAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewerAgent
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse


class AgentOrchestrator:
    def __init__(self) -> None:
        self.planner = PlannerAgent()
        self.coordinator = CoordinatorAgent()
        self.reviewer = ReviewerAgent()
        self.memory = MemoryAgent()

    def run(self, payload: AnalyzeRequest) -> AnalyzeResponse:
        tasks, planner_trace = self.planner.run(payload)
        tasks, coordinator_trace = self.coordinator.run(tasks)
        risks, reviewer_trace = self.reviewer.run(tasks)

        summary = "Generated an initial collaboration view from the latest message."
        memory_trace = self.memory.run(summary)

        return AnalyzeResponse(
            session_id=payload.session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=[
                "Confirm the owner for each task.",
                "Confirm concrete due dates in the Feishu chat.",
            ],
            agent_traces=[
                planner_trace,
                coordinator_trace,
                reviewer_trace,
                memory_trace,
            ],
        )
