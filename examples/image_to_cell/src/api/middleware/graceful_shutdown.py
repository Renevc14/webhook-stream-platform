"""
Graceful Shutdown Middleware
============================

Tracks active requests and provides graceful shutdown functionality.
Ensures in-flight requests complete before the application terminates.
"""

import asyncio
import logging
import time
from typing import Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


log = logging.getLogger(__name__)


class RequestTracker:
    """
    Tracks active HTTP requests for graceful shutdown.

    Thread-safe tracking of in-flight requests with support
    for waiting until all requests complete.
    """

    def __init__(self):
        self._active_requests: Set[str] = set()
        self._lock = asyncio.Lock()
        self._shutting_down = False
        self._request_counter = 0

    @property
    def active_count(self) -> int:
        """Number of currently active requests."""
        return len(self._active_requests)

    @property
    def is_shutting_down(self) -> bool:
        """Whether shutdown has been initiated."""
        return self._shutting_down

    async def start_request(self) -> str:
        """
        Register a new request.

        Returns:
            Request ID for tracking
        """
        async with self._lock:
            self._request_counter += 1
            request_id = f"req_{self._request_counter}_{int(time.time() * 1000)}"
            self._active_requests.add(request_id)
            return request_id

    async def end_request(self, request_id: str) -> None:
        """Mark a request as completed."""
        async with self._lock:
            self._active_requests.discard(request_id)

    async def wait_for_shutdown(
        self,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """
        Wait for all active requests to complete.

        Args:
            timeout: Maximum seconds to wait
            poll_interval: Seconds between checks

        Returns:
            True if all requests completed, False if timeout reached
        """
        self._shutting_down = True
        start_time = time.time()

        log.info(
            f"[SHUTDOWN] Waiting for {self.active_count} active requests to complete..."
        )

        while self.active_count > 0:
            elapsed = time.time() - start_time

            if elapsed >= timeout:
                log.warning(
                    f"[SHUTDOWN] Timeout reached with {self.active_count} "
                    f"requests still active"
                )
                return False

            remaining = timeout - elapsed
            log.debug(
                f"[SHUTDOWN] {self.active_count} requests active, "
                f"{remaining:.1f}s remaining"
            )

            await asyncio.sleep(poll_interval)

        log.info("[SHUTDOWN] All requests completed")
        return True

    def get_stats(self) -> dict:
        """Get current request statistics."""
        return {
            "active_requests": self.active_count,
            "total_requests": self._request_counter,
            "shutting_down": self._shutting_down,
        }


# Global request tracker singleton
_request_tracker: RequestTracker = None


def get_request_tracker() -> RequestTracker:
    """Get the global request tracker singleton."""
    global _request_tracker
    if _request_tracker is None:
        _request_tracker = RequestTracker()
    return _request_tracker


class GracefulShutdownMiddleware(BaseHTTPMiddleware):
    """
    Middleware that tracks requests for graceful shutdown.

    Also rejects new requests during shutdown with 503 status.
    """

    def __init__(self, app, reject_during_shutdown: bool = True):
        super().__init__(app)
        self._tracker = get_request_tracker()
        self._reject_during_shutdown = reject_during_shutdown

    async def dispatch(self, request: Request, call_next) -> Response:
        # Reject new requests during shutdown (except health checks)
        if self._reject_during_shutdown and self._tracker.is_shutting_down:
            if not request.url.path.startswith("/health"):
                log.warning(
                    f"[SHUTDOWN] Rejecting request to {request.url.path} - "
                    "shutdown in progress"
                )
                return Response(
                    content='{"error": "Service shutting down"}',
                    status_code=503,
                    media_type="application/json",
                    headers={"Retry-After": "30"},
                )

        # Track request
        request_id = await self._tracker.start_request()

        try:
            response = await call_next(request)
            return response
        finally:
            await self._tracker.end_request(request_id)
