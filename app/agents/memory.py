from app.schemas.analyze import AgentTrace


class MemoryAgent:
    def run(self, summary: str) -> AgentTrace:
        return AgentTrace(
            agent="memory",
            summary=f"Prepared this round for persistence: {summary}",
        )
