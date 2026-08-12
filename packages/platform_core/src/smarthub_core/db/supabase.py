"""
Supabase client wrapper for smarthub_core.

Solves the connection-exhaustion problem the legacy backends suffer at scale:

1. **Explicit httpx pool**: `supabase-py` defaults to httpx `max_connections=100`,
   `max_keepalive=20`. Multiplied across N workers this overruns Supabase's
   compute-micro 60-connection ceiling. We inject an `httpx.Client` configured
   from `CoreSettings` (`SUPABASE_MAX_CONNECTIONS`, `SUPABASE_MAX_KEEPALIVE`,
   `SUPABASE_KEEPALIVE_EXPIRY_S`) via `ClientOptions(httpx_client=...)`.

2. **Configurable semaphore**: legacy code hardcoded `threading.Semaphore(min(8, ...))`.
   We expose `SUPABASE_MAX_CONCURRENT_OPS` (default 4 — calibrated for compute
   micro; raise to 20 once PgBouncer is enabled).

3. **Async retry uses `asyncio.sleep`**: legacy code used `time.sleep` even in
   async paths, blocking the thread (and its httpx connection) during backoff.
   `execute_with_retry_async` releases the connection back to the pool while
   waiting.

4. **Expanded retryable-error patterns**: includes the HTTP/2 `ConnectionTerminated`
   case (Bug 6 — production observed), in addition to all legacy patterns.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Any, Callable, Optional, TypeVar

import httpx
from supabase import Client, ClientOptions, create_client

from smarthub_core.settings import CoreSettings, get_core_settings


log = logging.getLogger(__name__)

T = TypeVar("T")


class SupabaseError(Exception):
    """Raised when a Supabase operation exhausts retries or hits a fatal error."""


# Patterns that mark an exception as retryable. Lowercase substring match
# against `str(exception).lower()`. Includes legacy patterns plus HTTP/2 GOAWAY
# (`ConnectionTerminated`, `RemoteProtocolError`) discovered in production.
_RETRYABLE_ERRORS: frozenset[str] = frozenset(
    {
        # Connection-level
        "server disconnected",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "connection refused",
        "connection timed out",
        "broken pipe",
        # Read/timeout
        "timeout",
        "read timed out",
        # TLS
        "ssl error",
        # Windows-specific TCP errors (WinError 10053, 10054)
        "established connection was aborted",
        "winerror 10053",
        "winerror 10054",
        "software caused connection abort",
        "an existing connection was forcibly closed",
        # HTTP/2 — added after Bug 6 (production GOAWAY frame on connection recycle)
        "connectionterminated",
        "remoteprotocolerror",
    }
)


def is_retryable_error(exc: BaseException) -> bool:
    """True if the exception message matches a known transient pattern."""
    msg = str(exc).lower()
    return any(pat in msg for pat in _RETRYABLE_ERRORS)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SupabaseService:
    """Supabase client wrapper with explicit pool limits and retry helpers.

    Lifecycle:
        svc = SupabaseService(url, key)
        ...
        svc.close()                # sync — closes httpx client

    Or:
        async with SupabaseService(url, key) as svc:
            ...
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        settings: Optional[CoreSettings] = None,
    ):
        if not url or not key:
            raise SupabaseError("url and key are required")

        s = settings or get_core_settings()

        # 1. Build httpx client with explicit pool limits
        self._pool_limits: httpx.Limits = httpx.Limits(
            max_connections=s.supabase_max_connections,
            max_keepalive_connections=s.supabase_max_keepalive,
            keepalive_expiry=s.supabase_keepalive_expiry_s,
        )
        self._httpx_client: httpx.Client = httpx.Client(
            limits=self._pool_limits,
            timeout=httpx.Timeout(s.supabase_timeout_s),
            follow_redirects=True,
        )

        # 2. Create supabase client with our httpx_client injected.
        #
        # supabase-py < 2.9 accepted `ClientOptions(httpx_client=...)` which let
        # us inject a pool-bounded httpx.Client (see class docstring, point 1).
        # supabase-py >= 2.9 removed that kwarg and manages its own postgrest
        # httpx client internally, so we fall back gracefully. Pool control is
        # then bounded by supabase-py's defaults (max_connections=100), and our
        # `self._httpx_client` is retained for callers that use it directly
        # via the `httpx_client` property.
        try:
            options = ClientOptions(httpx_client=self._httpx_client)
        except TypeError:
            log.warning(
                "supabase-py >= 2.9 detected: 'httpx_client' injection is not "
                "supported; supabase-py will manage its own connection pool. "
                "For production, pin supabase-py < 2.9 or upstream a patch."
            )
            options = ClientOptions()
        self._client: Client = create_client(url, key, options=options)

        # 3. Configurable semaphores. asyncio variant is created lazily inside
        #    the running event loop on first use.
        self._sync_semaphore = threading.Semaphore(s.supabase_max_concurrent_ops)
        self._max_concurrent_ops = s.supabase_max_concurrent_ops
        self._async_semaphore: Optional[asyncio.Semaphore] = None
        self._async_semaphore_lock = threading.Lock()

        log.info(
            "supabase: initialized url=%s pool=(conn=%d, ka=%d, ka_expiry=%.0fs) sem=%d",
            _sanitize_url(url),
            s.supabase_max_connections,
            s.supabase_max_keepalive,
            s.supabase_keepalive_expiry_s,
            s.supabase_max_concurrent_ops,
        )

    # ----- properties -----------------------------------------------------

    @property
    def client(self) -> Client:
        return self._client

    def table(self, name: str):
        return self._client.table(name)

    def rpc(self, fn_name: str, params: Optional[dict] = None):
        """
        Proxy to ``self._client.rpc``. Exposed as a first-class method on the
        service so consumers can write ``svc.rpc("fn", {...}).execute()`` in
        the same style as ``svc.table("t").select(...).execute()`` without
        reaching into the underlying client.
        """
        return self._client.rpc(fn_name, params or {})

    @property
    def httpx_client(self) -> httpx.Client:
        return self._httpx_client

    @property
    def pool_limits(self) -> httpx.Limits:
        """The httpx.Limits in effect — useful for asserting configuration."""
        return self._pool_limits

    # ----- retry: sync ----------------------------------------------------

    def execute_with_retry(
        self,
        operation: Callable[[], T],
        description: str = "operation",
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> T:
        """Run a Supabase operation with exponential backoff retry.

        Acquires the sync semaphore for the duration of EACH attempt — and
        releases between attempts so the connection is not held during backoff.
        """
        last_error: Optional[BaseException] = None

        for attempt in range(max_retries + 1):
            try:
                with self._semaphore_guard_sync():
                    result = operation()
                if attempt > 0:
                    log.info("supabase: %s succeeded on attempt %d", description, attempt + 1)
                return result

            except Exception as e:
                last_error = e
                if not is_retryable_error(e) or attempt == max_retries:
                    log.error("supabase: %s failed: %s", description, e)
                    raise SupabaseError(f"{description} failed: {e}") from e

                delay = _backoff_delay(base_delay, attempt)
                log.warning(
                    "supabase: %s failed (attempt %d/%d): %s. Retrying in %.1fs",
                    description,
                    attempt + 1,
                    max_retries + 1,
                    e,
                    delay,
                )
                time.sleep(delay)

        raise SupabaseError(f"{description} failed after {max_retries} retries: {last_error}")

    # ----- retry: async ---------------------------------------------------

    async def execute_with_retry_async(
        self,
        operation: Callable[[], T],
        description: str = "operation",
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> T:
        """Async variant. Uses asyncio.Semaphore + asyncio.sleep so the worker
        does not block the event loop during backoff."""
        last_error: Optional[BaseException] = None

        for attempt in range(max_retries + 1):
            try:
                async with self._semaphore_guard_async():
                    result = operation()
                if attempt > 0:
                    log.info("supabase: %s succeeded on attempt %d", description, attempt + 1)
                return result

            except Exception as e:
                last_error = e
                if not is_retryable_error(e) or attempt == max_retries:
                    log.error("supabase: %s failed: %s", description, e)
                    raise SupabaseError(f"{description} failed: {e}") from e

                delay = _backoff_delay(base_delay, attempt)
                log.warning(
                    "supabase: %s failed (attempt %d/%d): %s. Retrying in %.1fs",
                    description,
                    attempt + 1,
                    max_retries + 1,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

        raise SupabaseError(f"{description} failed after {max_retries} retries: {last_error}")

    # ----- lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx client and release pool connections."""
        try:
            self._httpx_client.close()
        except Exception as e:  # pragma: no cover — defensive
            log.warning("supabase: error closing httpx client: %s", e)

    async def aclose(self) -> None:
        """Async close (runs sync close in a thread to avoid blocking the loop)."""
        await asyncio.to_thread(self.close)

    async def __aenter__(self) -> "SupabaseService":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ----- internal -------------------------------------------------------

    def _semaphore_guard_sync(self) -> "_SyncSemaphoreGuard":
        return _SyncSemaphoreGuard(self._sync_semaphore)

    def _semaphore_guard_async(self) -> "_AsyncSemaphoreGuard":
        if self._async_semaphore is None:
            with self._async_semaphore_lock:
                if self._async_semaphore is None:
                    self._async_semaphore = asyncio.Semaphore(self._max_concurrent_ops)
        return _AsyncSemaphoreGuard(self._async_semaphore)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _backoff_delay(base_delay: float, attempt: int) -> float:
    """Exponential backoff with up to 30% jitter."""
    delay = base_delay * (2**attempt)
    jitter = random.uniform(0.0, delay * 0.3)
    return delay + jitter


def _sanitize_url(url: str) -> str:
    """Strip credentials embedded in the URL for logging."""
    if "@" in url:
        return url.split("@", 1)[1]
    return url


class _SyncSemaphoreGuard:
    def __init__(self, sem: threading.Semaphore) -> None:
        self._sem = sem

    def __enter__(self) -> None:
        self._sem.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._sem.release()


class _AsyncSemaphoreGuard:
    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem

    async def __aenter__(self) -> None:
        await self._sem.acquire()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._sem.release()
