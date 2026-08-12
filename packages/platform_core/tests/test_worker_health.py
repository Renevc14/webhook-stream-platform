"""
Integration tests for smarthub_core.worker.health.

Use the real local Redis (via the `rc` fixture). Supabase checks are exercised
with a mocked SupabaseService — we don't need a real Supabase to validate the
report shape, the supabase: ok | error | not-configured branches, or the
status decision logic.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.worker.health import (
    HEALTH_DEGRADED,
    HEALTH_DOWN,
    HEALTH_OK,
    compute_health_report,
)


TEST_TOOL = "tw"
TEST_STREAM = f"stream:{TEST_TOOL}"
TEST_GROUP = TEST_TOOL


# ---------------------------------------------------------------------------
# Without Supabase (only Redis configured)
# ---------------------------------------------------------------------------


class TestWithoutSupabase:
    async def test_fresh_setup_reports_ok_with_zero_depth(self, rc: RedisClient):
        await rc.ensure_consumer_group(TEST_STREAM, TEST_GROUP)

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
        )
        assert report["status"] == HEALTH_OK
        assert report["tool"] == TEST_TOOL
        assert report["stream"] == TEST_STREAM
        assert report["group"] == TEST_GROUP
        assert report["queue_depth"] == 0
        assert report["pending"] == 0
        assert report["redis"] == HEALTH_OK
        assert report["supabase"] == "not-configured"

    async def test_unknown_stream_does_not_fail(self, rc: RedisClient):
        report = await compute_health_report(
            redis=rc,
            tool="never-existed",
            stream_name="stream:never-existed",
            group_name="ghost-group",
        )
        # Stream + group don't exist — that's OK (not deployed yet)
        assert report["status"] == HEALTH_OK
        assert report["queue_depth"] == 0
        assert report["pending"] == 0
        assert report["last_delivered_at"] is None

    async def test_queue_depth_reflects_unread_messages(self, rc: RedisClient):
        # Create the group first, THEN publish — messages are then "lag"
        await rc.ensure_consumer_group(TEST_STREAM, TEST_GROUP)
        for i in range(5):
            await rc.stream_publish(TEST_STREAM, {"v": "1", "event_id": f"e-{i}"})

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
        )
        assert report["queue_depth"] == 5
        assert report["pending"] == 0

    async def test_pending_reflects_unacked_messages(self, rc: RedisClient):
        await rc.ensure_consumer_group(TEST_STREAM, TEST_GROUP)
        for i in range(3):
            await rc.stream_publish(TEST_STREAM, {"v": "1", "event_id": f"e-{i}"})

        # Read but do NOT ACK — these become pending
        await rc.raw.xreadgroup(
            TEST_GROUP, "consumer-x",
            streams={TEST_STREAM: ">"},
            count=10, block=100,
        )

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
        )
        assert report["queue_depth"] == 0  # all delivered
        assert report["pending"] == 3       # but not acked

    async def test_high_pending_marks_degraded(self, rc: RedisClient):
        await rc.ensure_consumer_group(TEST_STREAM, TEST_GROUP)
        # Publish > threshold
        for i in range(15):
            await rc.stream_publish(TEST_STREAM, {"v": "1", "event_id": f"e-{i}"})
        await rc.raw.xreadgroup(
            TEST_GROUP, "consumer-x",
            streams={TEST_STREAM: ">"},
            count=100, block=100,
        )

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
            pending_threshold=10,  # tighten for the test
        )
        assert report["status"] == HEALTH_DEGRADED
        assert report["pending"] == 15

    async def test_last_delivered_at_isoformat_when_messages_consumed(self, rc: RedisClient):
        await rc.ensure_consumer_group(TEST_STREAM, TEST_GROUP)
        await rc.stream_publish(TEST_STREAM, {"v": "1", "event_id": "e-1"})
        await rc.raw.xreadgroup(
            TEST_GROUP, "c", streams={TEST_STREAM: ">"}, count=1, block=100
        )

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
        )
        assert report["last_delivered_at"] is not None
        # ISO 8601 UTC with Z suffix
        assert "T" in report["last_delivered_at"]
        assert report["last_delivered_at"].endswith("Z")


# ---------------------------------------------------------------------------
# With mocked Supabase
# ---------------------------------------------------------------------------


class TestWithSupabase:
    async def test_supabase_ok_passes_through(self, rc: RedisClient):
        svc = MagicMock(spec=SupabaseService)
        # Mock httpx HEAD response with 200
        response_mock = MagicMock()
        response_mock.status_code = 200
        svc.httpx_client = MagicMock()
        svc.httpx_client.head = MagicMock(return_value=response_mock)
        # Mock postgrest.base_url
        svc.client = MagicMock()
        svc.client.postgrest = MagicMock()
        svc.client.postgrest.base_url = "https://placeholder.supabase.co/rest/v1/"

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
            supabase=svc,
        )
        assert report["supabase"] == HEALTH_OK
        assert report["status"] == HEALTH_OK

    async def test_supabase_5xx_marks_degraded(self, rc: RedisClient):
        svc = MagicMock(spec=SupabaseService)
        response_mock = MagicMock()
        response_mock.status_code = 503
        svc.httpx_client = MagicMock()
        svc.httpx_client.head = MagicMock(return_value=response_mock)
        svc.client = MagicMock()
        svc.client.postgrest = MagicMock()
        svc.client.postgrest.base_url = "https://placeholder.supabase.co/rest/v1/"

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
            supabase=svc,
        )
        assert "error: status=503" in report["supabase"]
        assert report["status"] == HEALTH_DEGRADED

    async def test_supabase_transport_error_marks_degraded(self, rc: RedisClient):
        svc = MagicMock(spec=SupabaseService)
        svc.httpx_client = MagicMock()
        svc.httpx_client.head = MagicMock(side_effect=ConnectionError("network down"))
        svc.client = MagicMock()
        svc.client.postgrest = MagicMock()
        svc.client.postgrest.base_url = "https://placeholder.supabase.co/rest/v1/"

        report = await compute_health_report(
            redis=rc,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
            supabase=svc,
        )
        assert "error:" in report["supabase"]
        assert "ConnectionError" in report["supabase"]
        assert report["status"] == HEALTH_DEGRADED


# ---------------------------------------------------------------------------
# Redis down
# ---------------------------------------------------------------------------


class TestRedisDown:
    async def test_redis_unreachable_marks_down(self):
        # Build a client pointing at an unreachable Redis. We don't call start()
        # so health_check returns False. Then we patch the .raw access to raise
        # a RedisError if it gets that far.
        from smarthub_core.db.redis import RedisClient as _RC

        bad_client = _RC("redis://127.0.0.1:6399/15", socket_timeout_s=0.5)
        # Don't start() — the client will simply fail health_check

        report = await compute_health_report(
            redis=bad_client,
            tool=TEST_TOOL,
            stream_name=TEST_STREAM,
            group_name=TEST_GROUP,
        )
        assert report["status"] == HEALTH_DOWN
        assert report["redis"].startswith("error:")
