import logging
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.core.logging import _FrontendAccessLogFilter


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
        with patch.object(settings, "suppress_frontend_access_logs", True):
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
        with patch.object(settings, "suppress_frontend_access_logs", True):
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


if __name__ == "__main__":
    unittest.main()
