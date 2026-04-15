from app.agents.coordinator import CoordinatorAgent
from app.agents.memory import MemoryAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewerAgent
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.services.text_analysis import build_next_actions, build_summary


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

        summary = build_summary(payload.raw_text, tasks)
        next_actions = build_next_actions(tasks, risks)
        memory_trace = self.memory.run(summary)

        return AnalyzeResponse(
            session_id=payload.session_id,
            summary=summary,
            tasks=tasks,
            risks=risks,
            next_actions=next_actions,
            agent_traces=[
                planner_trace,
                coordinator_trace,
                reviewer_trace,
                memory_trace,
            ],
        )
