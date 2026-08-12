"""Shared pytest fixtures.

The Redis-backed fixtures connect to DB 15 (reserved for tests) so a `FLUSHDB`
between tests does not nuke any hand-testing state in DB 0. If Redis is not
reachable (no container running locally), the fixtures `pytest.skip` cleanly
so that pure-Python tests (e.g. crypto) still run.
"""

import os

import pytest
import pytest_asyncio

from smarthub_core.db.redis import RedisClient


REDIS_TEST_URL = os.environ.get(
    "REDIS_TEST_URL",
    "redis://127.0.0.1:6379/15",
)


@pytest_asyncio.fixture
async def rc():
    """Per-test RedisClient bound to DB 15. FLUSHDB before and after each test."""
    client = RedisClient(REDIS_TEST_URL, max_connections=10, socket_timeout_s=2.0)
    try:
        await client.start()
    except Exception as e:
        pytest.skip(f"Redis not reachable at {REDIS_TEST_URL}: {e}")
        return  # pragma: no cover

    try:
        await client.raw.flushdb()
        yield client
    finally:
        try:
            await client.raw.flushdb()
        except Exception:
            pass
        await client.close()
