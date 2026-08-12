"""Request correlation: one id per HTTP request, visible in every log line.

Without this a sandbox or staging log is a flat list of events that cannot be
tied back to the Hub action that caused them.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-Id"
NO_REQUEST = "-"

request_id_var: ContextVar[str] = ContextVar("smartqr_request_id", default=NO_REQUEST)


def get_request_id() -> str:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Expose the current request id to the formatter as `%(request_id)s`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
