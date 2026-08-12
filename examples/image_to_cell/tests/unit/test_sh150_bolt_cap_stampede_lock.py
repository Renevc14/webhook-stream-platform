"""SH-150 — bolt-cap Postgres fetch stampede lock."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services import bolt_cap


@pytest.fixture(autouse=True)
def _clear_usage_cache():
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache.clear()
    yield
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache.clear()


def _sample_ctx(org_id: str = "org-1") -> dict:
    return {
        "organization_id": org_id,
        "bolts_per_image": 15,
        "consumed_bolts_start": 0,
        "ceiling_bolts": 1000,
        "remaining_effective_bolts": 1000,
        "current_cycle_start": "2026-01-01T00:00:00Z",
    }


class TestLoadUsageContextAsync:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_redis(self):
        bolt_cap._store_usage_context_cache("org-1", _sample_ctx())
        client = MagicMock()
        client.acquire_lock = AsyncMock()

        with patch("src.services.redis_client.get_redis_client", return_value=client):
            ctx = await bolt_cap.load_i2c_org_usage_context_async("org-1")

        assert ctx["bolts_per_image"] == 15
        client.acquire_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_holder_fetches_once(self):
        client = MagicMock()
        client.acquire_lock = AsyncMock(return_value=True)
        client.release_lock = AsyncMock(return_value=True)

        with patch("src.services.redis_client.get_redis_client", return_value=client), patch(
            "src.services.bolt_cap._load_i2c_org_usage_context_with_retry",
            return_value=_sample_ctx(),
        ) as fetch:
            ctx = await bolt_cap.load_i2c_org_usage_context_async("org-1")

        assert ctx is not None
        fetch.assert_called_once_with("org-1")
        client.acquire_lock.assert_awaited_once()
        lock_name = client.acquire_lock.await_args.args[0]
        assert lock_name == "lock:fetch:bolt-cap:org-1"

    @pytest.mark.asyncio
    async def test_waiter_polls_cache_without_fetch(self):
        client = MagicMock()
        client.acquire_lock = AsyncMock(return_value=False)

        async def delayed_populate():
            await asyncio.sleep(0.05)
            bolt_cap._store_usage_context_cache("org-1", _sample_ctx())

        with patch("src.services.redis_client.get_redis_client", return_value=client), patch(
            "src.services.bolt_cap._load_i2c_org_usage_context_with_retry",
        ) as fetch, patch.object(bolt_cap, "_FETCH_LOCK_POLL_S", 0.02):
            populate_task = asyncio.create_task(delayed_populate())
            ctx = await bolt_cap.load_i2c_org_usage_context_async("org-1")
            await populate_task

        assert ctx is not None
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_redis_falls_back_to_sync_loader(self):
        with patch("src.services.redis_client.get_redis_client", return_value=None), patch(
            "src.services.bolt_cap.load_i2c_org_usage_context",
            return_value=_sample_ctx(),
        ) as sync_load:
            ctx = await bolt_cap.load_i2c_org_usage_context_async("org-1")

        assert ctx is not None
        sync_load.assert_called_once_with("org-1")
