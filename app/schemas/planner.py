from pydantic import BaseModel, Field


class PlannerStep(BaseModel):
    step_id: str
    step_type: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    notes: str | None = None


class ExecutionPlan(BaseModel):
    goal: str
    primary_intent: str
    steps: list[PlannerStep] = Field(default_factory=list)
