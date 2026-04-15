from __future__ import annotations

import logging
import urllib3


NOISY_HTTP_ACCESS_PATTERNS = (
    '/health HTTP/1.1"',
    '/status HTTP/1.1"',
    '/ibkr/status HTTP/1.1"',
    '/ibkr/monitor HTTP/1.1"',
    '/ibkr/account HTTP/1.1"',
    '/compute HTTP/1.1"',
    '/robots.txt HTTP/1.1"',
    '"GET / HTTP/1.1"',
)


class QuietEndpointAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(pattern in message for pattern in NOISY_HTTP_ACCESS_PATTERNS)


def configure_api_logging():
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(item, QuietEndpointAccessLogFilter) for item in werkzeug_logger.filters):
        werkzeug_logger.addFilter(QuietEndpointAccessLogFilter())
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
