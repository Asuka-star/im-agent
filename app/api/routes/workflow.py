from fastapi import APIRouter

from app.agents.orchestrator import AgentOrchestrator
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse


router = APIRouter()
orchestrator = AgentOrchestrator()


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    return orchestrator.run(payload)
