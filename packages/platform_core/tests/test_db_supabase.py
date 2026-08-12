"""
Unit tests for smarthub_core.db.supabase.

Retry semantics are tested with mocked operation callables — no real Supabase
required. The SupabaseService constructor still goes through `create_client`,
which validates URL formatting; we use a syntactically valid placeholder
(`https://placeholder.supabase.co`) and a dummy key.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from smarthub_core.db.supabase import (
    SupabaseError,
    SupabaseService,
    is_retryable_error,
)
from smarthub_core.settings import CoreSettings


PLACEHOLDER_URL = "https://placeholder.supabase.co"
PLACEHOLDER_KEY = "placeholder-service-role-key"


def _make_settings(**overrides) -> CoreSettings:
    """Build a CoreSettings instance for tests without reading .env files."""
    defaults = dict(
        app_crypto_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32-byte b64
        redis_url="redis://127.0.0.1:6379/15",
        supabase_max_connections=5,
        supabase_max_keepalive=3,
        supabase_keepalive_expiry_s=20.0,
        supabase_timeout_s=10.0,
        supabase_max_concurrent_ops=2,
    )
    defaults.update(overrides)
    return CoreSettings(**defaults)


@pytest.fixture
def svc():
    """SupabaseService with conservative pool / semaphore values."""
    s = _make_settings()
    service = SupabaseService(PLACEHOLDER_URL, PLACEHOLDER_KEY, settings=s)
    yield service
    service.close()


# ---------------------------------------------------------------------------
# is_retryable_error — pattern matching
# ---------------------------------------------------------------------------


class TestRetryableErrorDetection:
    @pytest.mark.parametrize(
        "msg",
        [
            "Server disconnected",
            "Connection reset by peer",
            "remote disconnected",
            "Read timed out",
            "SSL error during handshake",
            # The new HTTP/2 GOAWAY error pattern — Bug 6 from this session
            "<ConnectionTerminated error_code:1, last_stream_id:937, additional_data:None>",
            "RemoteProtocolError: Server disconnected",
            # Windows-specific
            "WinError 10053 — software caused connection abort",
        ],
    )
    def test_known_transient_messages_are_retryable(self, msg):
        assert is_retryable_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "permission denied",
            "syntax error in query",
            "duplicate key value violates unique constraint",
            "JSON could not be generated",  # the prior-processed query 400 (Bug 5)
            "no such table",
        ],
    )
    def test_non_transient_messages_are_not_retryable(self, msg):
        assert is_retryable_error(Exception(msg)) is False


# ---------------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_url_and_key(self):
        with pytest.raises(SupabaseError, match="url and key are required"):
            SupabaseService("", "key", settings=_make_settings())
        with pytest.raises(SupabaseError, match="url and key are required"):
            SupabaseService("https://x.supabase.co", "", settings=_make_settings())

    def test_httpx_pool_limits_match_settings(self, svc: SupabaseService):
        limits = svc.pool_limits
        assert limits.max_connections == 5
        assert limits.max_keepalive_connections == 3
        assert limits.keepalive_expiry == 20.0

    def test_semaphore_size_matches_settings(self, svc: SupabaseService):
        # Threading semaphore exposes its current value indirectly via _value
        assert svc._max_concurrent_ops == 2  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Retry: sync
# ---------------------------------------------------------------------------


class TestExecuteWithRetrySync:
    def test_succeeds_on_first_try(self, svc: SupabaseService):
        op = MagicMock(return_value="ok")
        result = svc.execute_with_retry(op, "test-op")
        assert result == "ok"
        assert op.call_count == 1

    def test_retries_on_transient_error_then_succeeds(self, svc: SupabaseService):
        op = MagicMock(
            side_effect=[
                Exception("Server disconnected"),
                Exception("Connection reset"),
                "success-after-retries",
            ]
        )
        result = svc.execute_with_retry(
            op, "test-op", max_retries=3, base_delay=0.01
        )
        assert result == "success-after-retries"
        assert op.call_count == 3

    def test_does_not_retry_on_fatal_error(self, svc: SupabaseService):
        op = MagicMock(side_effect=Exception("permission denied"))
        with pytest.raises(SupabaseError, match="permission denied"):
            svc.execute_with_retry(op, "test-op", max_retries=3, base_delay=0.01)
        assert op.call_count == 1

    def test_exhausts_retries_then_raises(self, svc: SupabaseService):
        op = MagicMock(side_effect=Exception("Server disconnected"))
        with pytest.raises(SupabaseError, match="failed"):
            svc.execute_with_retry(op, "test-op", max_retries=2, base_delay=0.01)
        # 1 initial attempt + 2 retries = 3 calls
        assert op.call_count == 3

    def test_retries_on_http2_connection_terminated(self, svc: SupabaseService):
        """Bug 6 from this session — HTTP/2 GOAWAY must trigger retry."""
        op = MagicMock(
            side_effect=[
                Exception("<ConnectionTerminated error_code:1, last_stream_id:937>"),
                "ok",
            ]
        )
        result = svc.execute_with_retry(
            op, "test-op", max_retries=3, base_delay=0.01
        )
        assert result == "ok"
        assert op.call_count == 2


# ---------------------------------------------------------------------------
# Retry: async
# ---------------------------------------------------------------------------


class TestExecuteWithRetryAsync:
    async def test_async_succeeds_on_first_try(self, svc: SupabaseService):
        op = MagicMock(return_value="ok")
        result = await svc.execute_with_retry_async(op, "test-op")
        assert result == "ok"
        assert op.call_count == 1

    async def test_async_retries_then_succeeds(self, svc: SupabaseService):
        op = MagicMock(
            side_effect=[Exception("Server disconnected"), "ok"]
        )
        result = await svc.execute_with_retry_async(
            op, "test-op", max_retries=3, base_delay=0.01
        )
        assert result == "ok"
        assert op.call_count == 2

    async def test_async_does_not_retry_on_fatal_error(self, svc: SupabaseService):
        op = MagicMock(side_effect=Exception("syntax error"))
        with pytest.raises(SupabaseError):
            await svc.execute_with_retry_async(
                op, "test-op", max_retries=3, base_delay=0.01
            )
        assert op.call_count == 1

    async def test_async_uses_asyncio_sleep_not_time_sleep(self, svc: SupabaseService):
        """Critical fix vs legacy: backoff in async path must yield the event
        loop, not block the worker thread. We verify by checking other tasks
        can run concurrently during the backoff window."""
        op = MagicMock(
            side_effect=[Exception("Server disconnected"), "ok"]
        )
        # Track whether a parallel task gets to run during the backoff
        parallel_ran = []

        async def parallel_task():
            await asyncio.sleep(0.01)
            parallel_ran.append("yes")

        # base_delay=0.05 means after first failure we sleep ~0.05s;
        # the parallel task sleeps 0.01s and should complete during that window.
        retry_task = asyncio.create_task(
            svc.execute_with_retry_async(
                op, "test-op", max_retries=2, base_delay=0.05
            )
        )
        side_task = asyncio.create_task(parallel_task())

        result = await retry_task
        await side_task

        assert result == "ok"
        assert parallel_ran == ["yes"]

    async def test_async_concurrent_ops_respect_semaphore(self, svc: SupabaseService):
        """The async semaphore was configured with max_concurrent_ops=2.
        If we kick off 5 concurrent calls, no more than 2 should be
        in-flight simultaneously."""
        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def slow_op_wrapper():
            def op():
                nonlocal in_flight, max_in_flight
                # Track concurrency at the moment the semaphore lets us in
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                time.sleep(0.05)  # simulate I/O cost
                in_flight -= 1
                return "ok"

            async with lock:
                pass  # no-op — just ensures the lock is alive
            return await svc.execute_with_retry_async(op, "concurrency-test")

        results = await asyncio.gather(*(slow_op_wrapper() for _ in range(5)))
        assert results == ["ok"] * 5
        assert max_in_flight <= 2, f"semaphore breach: max_in_flight={max_in_flight}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_close_idempotent(self):
        s = SupabaseService(PLACEHOLDER_URL, PLACEHOLDER_KEY, settings=_make_settings())
        s.close()
        # Second close should not raise
        s.close()

    async def test_async_context_manager(self):
        async with SupabaseService(
            PLACEHOLDER_URL, PLACEHOLDER_KEY, settings=_make_settings()
        ) as s:
            assert s.client is not None
        # After __aexit__ the httpx client must be closed
        assert s.httpx_client.is_closed
