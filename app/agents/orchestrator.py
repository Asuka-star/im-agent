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
        tasks, planner_trace, llm_result = self.planner.run(payload)
        tasks, coordinator_trace = self.coordinator.run(tasks)
        if llm_result and isinstance(llm_result.get("risks"), list):
            risks = [str(item) for item in llm_result["risks"] if str(item).strip()]
            reviewer_trace = self.reviewer.run(tasks)[1]
        else:
            risks, reviewer_trace = self.reviewer.run(tasks)

        summary = str(llm_result.get("summary")).strip() if llm_result and llm_result.get("summary") else build_summary(payload.raw_text, tasks)
        next_actions = (
            [str(item) for item in llm_result["next_actions"] if str(item).strip()][:4]
            if llm_result and isinstance(llm_result.get("next_actions"), list) and llm_result.get("next_actions")
            else build_next_actions(tasks, risks)
        )
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
