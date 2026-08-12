"""SH-99 — cross-process webhook dedup service.

Pins the contract of ``WebhookDedupService``:

  * ``try_acquire_processing`` returns True only for the first caller per
    ``(attachment_id, row_id)`` within the TTL window, even when callers are
    concurrent (atomicity guaranteed by Redis ``SET NX EX``).
  * ``is_attachment_completed`` reflects writes from
    ``mark_attachment_completed`` immediately.
  * On ``RedisError`` (mid-operation), each method falls back to the
    in-memory dict and emits a WARN — service stays usable, dedup stays
    "per-process only" until Redis comes back.
  * If the service is instantiated with ``client=None`` (startup degradation
    path), all operations use the in-memory dicts directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from src.services.webhook_dedup import (
    WebhookDedupService,
    _COMPLETED_PREFIX,
    _PROCESSING_PREFIX,
)


# ---------------------------------------------------------------------------
# Redis-backed path
# ---------------------------------------------------------------------------


def _client_with_raw(raw_mock):
    """Build a RedisClient double whose `.raw` attribute is the given mock.

    Mirrors the smarthub_core RedisClient surface used by the service:
    ``client.raw.set(...)``, ``client.raw.exists(...)``.
    """
    client = MagicMock()
    client.raw = raw_mock
    return client


class TestTryAcquireProcessingRedis:
    @pytest.mark.asyncio
    async def test_first_caller_wins(self):
        raw = MagicMock()
        raw.set = AsyncMock(return_value=True)  # SET NX EX → key newly set
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        ok = await svc.try_acquire_processing(attachment_id=42, row_id=7)

        assert ok is True
        raw.set.assert_awaited_once_with(
            f"{_PROCESSING_PREFIX}42:7", "1", nx=True, ex=600
        )

    @pytest.mark.asyncio
    async def test_second_caller_loses(self):
        raw = MagicMock()
        raw.set = AsyncMock(return_value=None)  # SET NX returns None when key exists
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        ok = await svc.try_acquire_processing(attachment_id=42, row_id=7)
        assert ok is False

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_on_redis_error(self, caplog):
        raw = MagicMock()
        raw.set = AsyncMock(side_effect=RedisError("connection refused"))
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        with caplog.at_level("WARNING"):
            ok1 = await svc.try_acquire_processing(attachment_id=42, row_id=7)
            ok2 = await svc.try_acquire_processing(attachment_id=42, row_id=7)

        # First call wins via the in-memory fallback; the second one sees the
        # in-memory entry and loses. Service stays correct in degraded mode.
        assert ok1 is True
        assert ok2 is False
        assert any(
            "try_acquire_processing Redis error" in r.message
            for r in caplog.records
        )


class TestCompletedRedis:
    @pytest.mark.asyncio
    async def test_mark_then_check(self):
        raw = MagicMock()
        raw.set = AsyncMock(return_value=True)
        raw.exists = AsyncMock(return_value=1)
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        await svc.mark_attachment_completed(attachment_id=99)
        raw.set.assert_awaited_once_with(
            f"{_COMPLETED_PREFIX}99", "1", ex=600
        )

        assert await svc.is_attachment_completed(99) is True
        raw.exists.assert_awaited_once_with(f"{_COMPLETED_PREFIX}99")

    @pytest.mark.asyncio
    async def test_exists_returns_false_when_missing(self):
        raw = MagicMock()
        raw.exists = AsyncMock(return_value=0)
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        assert await svc.is_attachment_completed(99) is False

    @pytest.mark.asyncio
    async def test_is_completed_falls_back_to_memory_on_redis_error(self, caplog):
        raw = MagicMock()
        raw.set = AsyncMock(side_effect=RedisError("boom"))
        raw.exists = AsyncMock(side_effect=RedisError("boom"))
        svc = WebhookDedupService(client=_client_with_raw(raw), ttl_s=600)

        # Mark fails on Redis → falls back to in-memory dict.
        with caplog.at_level("WARNING"):
            await svc.mark_attachment_completed(123)
            # Subsequent check also fails on Redis → reads in-memory dict.
            assert await svc.is_attachment_completed(123) is True

        assert any(
            "mark_attachment_completed Redis error" in r.message
            for r in caplog.records
        )
        assert any(
            "is_attachment_completed Redis error" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# In-memory-only path (Redis was unavailable at startup, client=None)
# ---------------------------------------------------------------------------


class TestInMemoryOnly:
    def test_using_redis_property(self):
        svc_mem = WebhookDedupService(client=None, ttl_s=600)
        assert svc_mem.using_redis is False

        svc_redis = WebhookDedupService(client=MagicMock(), ttl_s=600)
        assert svc_redis.using_redis is True

    @pytest.mark.asyncio
    async def test_try_acquire_in_memory(self):
        svc = WebhookDedupService(client=None, ttl_s=600)

        assert await svc.try_acquire_processing(1, 2) is True
        assert await svc.try_acquire_processing(1, 2) is False
        # Different keys are independent.
        assert await svc.try_acquire_processing(1, 3) is True

    @pytest.mark.asyncio
    async def test_completed_in_memory_roundtrip(self):
        svc = WebhookDedupService(client=None, ttl_s=600)

        assert await svc.is_attachment_completed(7) is False
        await svc.mark_attachment_completed(7)
        assert await svc.is_attachment_completed(7) is True


# ---------------------------------------------------------------------------
# TTL semantics on the in-memory fallback (Redis handles its own TTL)
# ---------------------------------------------------------------------------


class TestInMemoryTtlExpiry:
    @pytest.mark.asyncio
    async def test_processing_expires_after_ttl(self, monkeypatch):
        import src.services.webhook_dedup as dedup_module

        fake_now = {"t": 1000.0}
        monkeypatch.setattr(dedup_module.time, "time", lambda: fake_now["t"])

        svc = WebhookDedupService(client=None, ttl_s=60)

        assert await svc.try_acquire_processing(1, 2) is True
        # Within TTL — still owned by the previous caller.
        fake_now["t"] += 30
        assert await svc.try_acquire_processing(1, 2) is False
        # Past TTL — previous claim expired, a new caller can acquire.
        fake_now["t"] += 31
        assert await svc.try_acquire_processing(1, 2) is True

    @pytest.mark.asyncio
    async def test_completed_expires_after_ttl(self, monkeypatch):
        import src.services.webhook_dedup as dedup_module

        fake_now = {"t": 1000.0}
        monkeypatch.setattr(dedup_module.time, "time", lambda: fake_now["t"])

        svc = WebhookDedupService(client=None, ttl_s=60)

        await svc.mark_attachment_completed(99)
        assert await svc.is_attachment_completed(99) is True
        fake_now["t"] += 61
        assert await svc.is_attachment_completed(99) is False
