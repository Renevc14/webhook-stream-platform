"""
SH-149 — cross-replica webhook dedup against a real Redis.

Opt-in: set REDIS_URL (or SMARTHUB_E2E_REDIS=1 with local Redis on 6379).
Skips when Redis is unreachable — same pattern as smarthub_core e2e tests.

This does NOT require ngrok or two Docker containers; it proves the same
atomic SET NX contract two ECS tasks rely on.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_E2E_FLAG = os.environ.get("SMARTHUB_E2E_REDIS", "").strip() in ("1", "true", "yes")


def _should_run() -> bool:
    return _E2E_FLAG or bool(os.environ.get("REDIS_URL"))


@pytest_asyncio.fixture
async def redis_client():
    if not _should_run():
        pytest.skip("Set REDIS_URL or SMARTHUB_E2E_REDIS=1 to run SH-149 integration test")

    from smarthub_core.db.redis import RedisClient

    client = RedisClient(_REDIS_URL)
    try:
        await client.start()
    except Exception as exc:
        pytest.skip(f"Redis not reachable at {_REDIS_URL}: {exc}")
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_two_workers_one_processing_slot(redis_client):
    """Concurrent try_acquire from two services → exactly one winner."""
    from src.services.webhook_dedup import WebhookDedupService

    attachment_id = 9_001_002_003
    row_id = 0
    svc_a = WebhookDedupService(redis_client)
    svc_b = WebhookDedupService(redis_client)

    results = await asyncio.gather(
        svc_a.try_acquire_processing(attachment_id, row_id),
        svc_b.try_acquire_processing(attachment_id, row_id),
    )

    assert results.count(True) == 1
    assert results.count(False) == 1


@pytest.mark.asyncio
async def test_burst_acquires_single_winner(redis_client):
    """N concurrent workers racing for the same slot → exactly one winner."""
    from src.services.webhook_dedup import WebhookDedupService

    attachment_id = 9_001_002_004
    row_id = 0
    services = [WebhookDedupService(redis_client) for _ in range(10)]

    results = await asyncio.gather(
        *[svc.try_acquire_processing(attachment_id, row_id) for svc in services]
    )

    assert results.count(True) == 1
    assert results.count(False) == 9
