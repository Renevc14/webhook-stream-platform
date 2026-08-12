"""
Authentication Middleware
=========================

Handles authentication for API endpoints.
"""

import logging
from typing import Optional, Set

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.config.settings import get_settings


log = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Authentication middleware.

    Validates requests based on configured authentication method.
    """

    def __init__(
        self,
        app,
        public_paths: Optional[Set[str]] = None,
        webhook_paths: Optional[Set[str]] = None,
    ):
        """
        Initialize auth middleware.

        Args:
            app: FastAPI application
            public_paths: Paths that don't require authentication
            webhook_paths: Webhook paths (use signature verification instead)
        """
        super().__init__(app)
        self.public_paths = public_paths or {"/health", "/ready", "/docs", "/openapi.json"}
        self.webhook_paths = webhook_paths or {"/webhook"}
        self._settings = get_settings()

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process request with authentication check."""
        path = request.url.path

        # Allow public paths
        if path in self.public_paths:
            return await call_next(request)

        # Webhook paths use signature verification (handled in route)
        if any(path.startswith(wp) for wp in self.webhook_paths):
            return await call_next(request)

        # Hub session already verified by Supabase JWT middleware
        if getattr(request.state, "i2c_jwt_sub", None):
            return await call_next(request)

        # Check API key if configured
        if self._settings.api_key:
            api_key = request.headers.get("X-API-Key")

            if not api_key:
                log.warning(f"Missing API key for {path}")
                return JSONResponse(
                    status_code=401,
                    content={"error": "API key required"},
                )

            if api_key != self._settings.api_key:
                log.warning(f"Invalid API key for {path}")
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid API key"},
                )

        # Request is authenticated
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Request logging middleware.

    Logs all incoming requests with timing information.
    """

    def __init__(self, app, log_body: bool = False):
        """
        Initialize logging middleware.

        Args:
            app: FastAPI application
            log_body: Whether to log request bodies
        """
        super().__init__(app)
        self.log_body = log_body

    async def dispatch(self, request: Request, call_next) -> Response:
        """Log request and response."""
        import time

        start_time = time.time()

        # Log request
        log.info(
            f"Request: {request.method} {request.url.path} "
            f"from {request.client.host if request.client else 'unknown'}"
        )

        # Process request
        response = await call_next(request)

        # Log response
        duration_ms = int((time.time() - start_time) * 1000)
        log.info(
            f"Response: {request.method} {request.url.path} "
            f"status={response.status_code} duration={duration_ms}ms"
        )

        return response
