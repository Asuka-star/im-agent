from pydantic import BaseModel


class SessionState(BaseModel):
    session_id: str
    summary: str
