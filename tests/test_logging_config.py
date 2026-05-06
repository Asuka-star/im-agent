import logging
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.core.logging import _FrontendAccessLogFilter, _access_log_enabled, _ensure_frontend_filter, configure_logging


class FrontendAccessLogFilterTests(unittest.TestCase):
    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            message,
            (),
            None,
        )

    def test_suppresses_successful_frontend_polling_logs(self) -> None:
        filter_ = _FrontendAccessLogFilter()
        with patch.object(settings, "log_frontend_requests", False), patch.object(
            settings,
            "suppress_frontend_access_logs",
            True,
        ):
            self.assertFalse(
                filter_.filter(
                    self._record('127.0.0.1:1 - "GET /api/task-runs/run_1 HTTP/1.1" 200 OK')
                )
            )
            self.assertFalse(
                filter_.filter(
                    self._record('127.0.0.1:1 - "GET /api/requirements/req_1 HTTP/1.1" 200 OK')
                )
            )

    def test_keeps_errors_and_non_frontend_logs(self) -> None:
        filter_ = _FrontendAccessLogFilter()
        with patch.object(settings, "log_frontend_requests", False), patch.object(
            settings,
            "suppress_frontend_access_logs",
            True,
        ):
            self.assertTrue(
                filter_.filter(
                    self._record('127.0.0.1:1 - "GET /api/task-runs/run_1 HTTP/1.1" 404 Not Found')
                )
            )
            self.assertTrue(
                filter_.filter(
                    self._record('127.0.0.1:1 - "POST /api/feishu/events HTTP/1.1" 200 OK')
                )
            )

    def test_can_enable_frontend_request_logs(self) -> None:
        filter_ = _FrontendAccessLogFilter()
        with patch.object(settings, "log_frontend_requests", True), patch.object(
            settings,
            "suppress_frontend_access_logs",
            True,
        ):
            self.assertTrue(_access_log_enabled())
            self.assertTrue(
                filter_.filter(
                    self._record('127.0.0.1:1 - "GET /api/task-runs/run_1 HTTP/1.1" 200 OK')
                )
            )
            self.assertTrue(filter_.filter(self._record("connection open")))

    def test_legacy_suppress_switch_can_disable_filtering(self) -> None:
        filter_ = _FrontendAccessLogFilter()
        with patch.object(settings, "log_frontend_requests", False), patch.object(
            settings,
            "suppress_frontend_access_logs",
            False,
        ):
            self.assertTrue(_access_log_enabled())
            self.assertTrue(
                filter_.filter(
                    self._record('127.0.0.1:1 - "GET /api/requirements/req_1 HTTP/1.1" 200 OK')
                )
            )

    def test_installs_filter_on_logger_and_handlers(self) -> None:
        logger = logging.getLogger("tests.frontend-access-filter")
        logger.filters.clear()
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        _ensure_frontend_filter(logger)
        _ensure_frontend_filter(logger)

        self.assertEqual(
            sum(isinstance(item, _FrontendAccessLogFilter) for item in logger.filters),
            1,
        )
        self.assertEqual(
            sum(isinstance(item, _FrontendAccessLogFilter) for item in handler.filters),
            1,
        )

    def test_configure_logging_disables_uvicorn_access_by_default(self) -> None:
        access_logger = logging.getLogger("uvicorn.access")
        original_disabled = access_logger.disabled
        self.addCleanup(setattr, access_logger, "disabled", original_disabled)

        with patch.object(settings, "log_frontend_requests", False), patch.object(
            settings,
            "suppress_frontend_access_logs",
            True,
        ):
            configure_logging()

        self.assertTrue(access_logger.disabled)

    def test_configure_logging_keeps_uvicorn_access_when_enabled(self) -> None:
        access_logger = logging.getLogger("uvicorn.access")
        original_disabled = access_logger.disabled
        self.addCleanup(setattr, access_logger, "disabled", original_disabled)

        with patch.object(settings, "log_frontend_requests", True), patch.object(
            settings,
            "suppress_frontend_access_logs",
            True,
        ):
            configure_logging()

        self.assertFalse(access_logger.disabled)


if __name__ == "__main__":
    unittest.main()
