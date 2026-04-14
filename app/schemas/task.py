from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    title: str = Field(..., description="Task title")
    owner: str = Field(default="TBD", description="Proposed task owner")
    priority: str = Field(default="medium", description="Task priority")
    due_date: str = Field(default="TBD", description="Suggested due date")
    status: str = Field(default="draft", description="Task status")
    notes: str = Field(default="", description="Additional notes")
