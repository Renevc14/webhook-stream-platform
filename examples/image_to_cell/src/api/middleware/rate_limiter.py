"""
Rate Limiter Middleware
=======================

Implements rate limiting for API endpoints.
"""

import time
import logging
from typing import Dict, Optional
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


log = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Track rate limit state for a key."""
    tokens: float = 0.0
    last_update: float = field(default_factory=time.time)


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Token bucket rate limiter middleware.

    Implements per-IP rate limiting. Webhook paths use higher limits for DDoS
    protection while allowing Smartsheet's burst traffic.
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = 60,
        burst_size: int = 10,
        exclude_paths: Optional[list] = None,
        webhook_requests_per_minute: int = 100,
        webhook_burst_size: int = 20,
    ):
        """
        Initialize rate limiter.

        Args:
            app: FastAPI application
            requests_per_minute: Sustained request rate for API
            burst_size: Maximum burst size for API
            exclude_paths: Paths to exclude from rate limiting (e.g. /health)
            webhook_requests_per_minute: Higher limit for webhook (DDoS protection)
            webhook_burst_size: Burst for webhook (Smartsheet sends bursts)
        """
        super().__init__(app)
        self.api_rate = requests_per_minute / 60.0
        self.api_burst = burst_size
        self.webhook_rate = webhook_requests_per_minute / 60.0
        self.webhook_burst = webhook_burst_size
        self.exclude_paths = exclude_paths or ["/health", "/ready"]
        self.webhook_prefixes = ["/webhook"]
        self._buckets: Dict[str, RateLimitState] = {}
        self._cleanup_interval = 300  # 5 minutes
        self._last_cleanup = time.time()

    def _get_bucket_key(self, client_key: str, is_webhook: bool) -> str:
        """Separate buckets for API vs webhook per client."""
        return f"{client_key}:webhook" if is_webhook else f"{client_key}:api"

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process request with rate limiting."""
        path = request.url.path

        # Skip excluded exact paths
        if path in self.exclude_paths:
            return await call_next(request)

        is_webhook = any(path.startswith(p) for p in self.webhook_prefixes)
        rate = self.webhook_rate if is_webhook else self.api_rate
        burst = self.webhook_burst if is_webhook else self.api_burst

        client_key = self._get_client_key(request)
        bucket_key = self._get_bucket_key(client_key, is_webhook)

        if not self._check_rate_limit(bucket_key, rate, burst):
            log.warning("Rate limit exceeded for %s", "[REDACTED]" if is_webhook else bucket_key)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "retry_after": int(1 / rate),
                },
                headers={"Retry-After": str(int(1 / rate))},
            )

        response = await call_next(request)

        bucket = self._buckets.get(bucket_key)
        if bucket:
            response.headers["X-RateLimit-Remaining"] = str(int(bucket.tokens))
            response.headers["X-RateLimit-Limit"] = str(int(burst))

        return response

    def _get_client_key(self, request: Request) -> str:
        """Get unique client identifier."""
        # Try to get real IP from proxy headers
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # Fall back to direct client IP
        if request.client:
            return request.client.host

        return "unknown"

    def _check_rate_limit(self, bucket_key: str, rate: float, burst: float) -> bool:
        """
        Check if request is within rate limit.

        Uses token bucket algorithm.
        """
        now = time.time()
        if bucket_key not in self._buckets:
            self._buckets[bucket_key] = RateLimitState(tokens=burst, last_update=now)

        bucket = self._buckets[bucket_key]

        # Add tokens based on time elapsed
        elapsed = now - bucket.last_update
        bucket.tokens = min(burst, bucket.tokens + elapsed * rate)
        bucket.last_update = now

        # Check if we have a token
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            self._maybe_cleanup(now)
            return True

        return False

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically clean up old entries."""
        if now - self._last_cleanup > self._cleanup_interval:
            # Remove entries older than cleanup interval
            cutoff = now - self._cleanup_interval
            expired = [
                k for k, v in self._buckets.items()
                if v.last_update < cutoff
            ]
            for k in expired:
                del self._buckets[k]
            self._last_cleanup = now
