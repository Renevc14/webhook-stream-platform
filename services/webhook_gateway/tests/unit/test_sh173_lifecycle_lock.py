"""SH-173 — lifecycle singleton lock exclusivity."""

import asyncio

import pytest

from gateway.lifecycle.lock import LIFECYCLE_LOCK_KEY, hold_lifecycle_lock


class FakeRedis:
    def __init__(self):
        self._locks = {}

    async def acquire_lock(self, name, token, ttl_s):
        if name in self._locks:
            return False
        self._locks[name] = token
        return True

    async def extend_lock(self, name, token, ttl_s):
        return self._locks.get(name) == token

    async def release_lock(self, name, token):
        if self._locks.get(name) == token:
            del self._locks[name]
            return True
        return False


@pytest.mark.asyncio
async def test_second_holder_skipped():
    redis = FakeRedis()
    results = []

    async def holder(label, hold_s):
        async with hold_lifecycle_lock(
            redis, ttl_s=30, heartbeat_interval_s=100
        ) as acquired:
            results.append((label, acquired))
            if acquired:
                await asyncio.sleep(hold_s)

    t1 = asyncio.create_task(holder("a", 0.2))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(holder("b", 0.05))
    await asyncio.gather(t1, t2)
    assert ("a", True) in results
    assert ("b", False) in results
    assert LIFECYCLE_LOCK_KEY not in redis._locks


@pytest.mark.asyncio
async def test_no_redis_yields_true():
    async with hold_lifecycle_lock(None) as acquired:
        assert acquired is True
