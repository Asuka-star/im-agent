from pydantic import BaseModel, Field

from app.schemas.task import TaskItem


class AnalyzeRequest(BaseModel):
    session_id: str = Field(..., description="Conversation session identifier")
    raw_text: str = Field(..., description="Raw discussion content from Feishu chat")


class AgentTrace(BaseModel):
    agent: str
    summary: str


class AnalyzeResponse(BaseModel):
    session_id: str
    summary: str
    tasks: list[TaskItem]
    risks: list[str]
    next_actions: list[str]
    agent_traces: list[AgentTrace]
