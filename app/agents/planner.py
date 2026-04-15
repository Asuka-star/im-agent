from app.schemas.analyze import AgentTrace, AnalyzeRequest
from app.schemas.task import TaskItem
from app.services.llm import LLMService
from app.services.text_analysis import extract_tasks
import logging


logger = logging.getLogger(__name__)


class PlannerAgent:
    def __init__(self) -> None:
        self.llm_service = LLMService()

    def run(self, payload: AnalyzeRequest) -> tuple[list[TaskItem], AgentTrace, dict | None]:
        if self.llm_service.is_configured():
            try:
                llm_result = self.llm_service.extract_collaboration(payload.raw_text)
                tasks = [
                    TaskItem.model_validate(task)
                    for task in llm_result.get("tasks", [])
                    if isinstance(task, dict)
                ]
                if tasks:
                    logger.info("Planner used LLM extraction and produced %s task(s)", len(tasks))
                    trace = AgentTrace(
                        agent="planner",
                        summary=f"LLM extracted {len(tasks)} task candidate(s) from the latest discussion.",
                    )
                    return tasks, trace, llm_result
            except Exception as exc:  # noqa: BLE001
                logger.warning("Planner LLM extraction failed, falling back to rules: %s", exc)
                trace = AgentTrace(
                    agent="planner",
                    summary=f"LLM extraction failed, falling back to rule-based parsing: {exc}",
                )
                tasks = extract_tasks(payload.raw_text)
                return tasks, trace, None

        logger.info("Planner used rule-based extraction")
        tasks = extract_tasks(payload.raw_text)
        trace = AgentTrace(
            agent="planner",
            summary=f"Extracted {len(tasks)} initial task candidate(s) from the latest discussion.",
        )
        return tasks, trace, None
