import logging

from app.core.config import settings


class _FrontendAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not settings.suppress_frontend_access_logs:
            return True
        message = record.getMessage()
        if self._is_successful_frontend_poll(message):
            return False
        if '"WebSocket /api/ws/task-runs' in message:
            return False
        if message in {"connection open", "connection closed"}:
            return False
        return True

    @staticmethod
    def _is_successful_frontend_poll(message: str) -> bool:
        if '" 200 OK' not in message:
            return False
        return any(
            pattern in message
            for pattern in (
                '"GET /api/task-runs/',
                '"GET /api/requirements/',
            )
        )


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _FrontendAccessLogFilter) for item in logger.filters):
            logger.addFilter(_FrontendAccessLogFilter())
