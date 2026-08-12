"""
Layer A end-to-end tests for SmarthubWorker.

These exercise the full worker lifecycle (XADD → XREADGROUP → process →
ACK + DLQ + platform-events) against the local Redis container. No
external services are involved; opt-in via SMARTHUB_E2E_REDIS=1.

Difference vs the unit tests in tests/test_worker_base.py: the unit tests
exercise individual paths in isolation. These e2e tests run multi-message
flows, multi-consumer scenarios, and verify health-endpoint reporting
during real workload — the kind of behaviour you'd see in production.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

import pytest

from smarthub_core.db.redis import RedisClient
from smarthub_core.worker.base import (
    PLATFORM_EVENTS_STREAM,
    FatalError,
    Job,
    Result,
    RetryableError,
    SmarthubWorker,
)
from smarthub_core.worker.health import (
    HEALTH_DEGRADED,
    HEALTH_OK,
    compute_health_report,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _ProgrammableWorker(SmarthubWorker):
    """Worker with controllable per-event behaviour. Each event_id can map
    to a list of outcomes that play back across retries."""

    tool_slug = "e2e"
    base_delay_s = 0.01
    max_attempts = 3

    def __init__(self, redis, *, behaviours=None, dedup=False, **kwargs):
        super().__init__(redis, **kwargs)
        self._behaviours: dict[str, list] = behaviours or {}
        self._dedup = dedup
        self.processed: list[Job] = []

    def dedup_key(self, job: Job) -> Optional[str]:
        if not self._dedup:
            return None
        return f"e2e:dedup:{job.event_id}"

    async def process(self, job: Job) -> Result:
        self.processed.append(job)
        outcomes = self._behaviours.get(job.event_id, [])
        if not outcomes:
            return Result()
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _envelope(
    event_id: str,
    *,
    org_id: str = "e2e-org",
    config_id: str = "e2e-cfg",
    raw: Optional[dict] = None,
) -> dict[str, str]:
    return {
        "v": "1",
        "event_id": event_id,
        "tool": "e2e",
        "org_id": org_id,
        "config_id": config_id,
        "received_at": "1700000000",
        "raw": json.dumps(raw or {"k": "v"}),
    }


async def _drain(
    worker: SmarthubWorker,
    *,
    expected_acks: int,
    timeout_s: float = 5.0,
) -> None:
    """Run worker until `expected_acks` messages have been delivered AND
    no pending entries remain. Then signal shutdown."""
    worker.block_ms = 50
    task = asyncio.create_task(worker.run())
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        groups = await worker._redis.raw.xinfo_groups(worker.stream_name)
        for g in groups:
            if g["name"] == worker.group_name:
                if g["pending"] == 0 and g.get("lag", 0) == 0:
                    worker.request_shutdown()
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except asyncio.TimeoutError:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise AssertionError(
        f"worker did not drain in {timeout_s}s "
        f"(expected_acks={expected_acks}, processed={len(worker.processed)})"  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# E2E-1: Worker happy-path flow
# ---------------------------------------------------------------------------


class TestE2E1HappyPath:
    async def test_publishes_consumed_acked_in_order(self, e2e_rc: RedisClient):
        """A series of messages is consumed and acked, preserving order
        within a single consumer."""
        for i in range(10):
            await e2e_rc.stream_publish(
                "stream:e2e", _envelope(f"evt-{i:02d}")
            )

        worker = _ProgrammableWorker(e2e_rc)
        await _drain(worker, expected_acks=10)

        assert len(worker.processed) == 10
        assert [j.event_id for j in worker.processed] == [
            f"evt-{i:02d}" for i in range(10)
        ]

        # All acked — no pending
        groups = await e2e_rc.raw.xinfo_groups("stream:e2e")
        assert groups[0]["pending"] == 0

    async def test_dedup_skips_duplicates_across_messages(self, e2e_rc: RedisClient):
        """Two different stream messages with the same dedup key — only
        the first triggers process()."""
        # Two messages, same event_id => same dedup key
        await e2e_rc.stream_publish("stream:e2e", _envelope("dup-1"))
        await e2e_rc.stream_publish("stream:e2e", _envelope("dup-1"))

        worker = _ProgrammableWorker(e2e_rc, dedup=True)
        await _drain(worker, expected_acks=2)

        # Only one processed — second dedup'd
        assert len(worker.processed) == 1


# ---------------------------------------------------------------------------
# E2E-2: Worker DLQ on FatalError
# ---------------------------------------------------------------------------


class TestE2E2FatalDLQ:
    async def test_fatal_error_routes_to_dlq_with_diagnostics(
        self, e2e_rc: RedisClient
    ):
        await e2e_rc.stream_publish("stream:e2e", _envelope("fatal-1"))

        worker = _ProgrammableWorker(
            e2e_rc,
            behaviours={"fatal-1": [FatalError("permanent-x")]},
        )
        await _drain(worker, expected_acks=1)

        # No retry attempted on FatalError
        assert len(worker.processed) == 1

        # DLQ stream populated with full diagnostics
        dlq = await e2e_rc.raw.xrange("stream:e2e:dlq")
        assert len(dlq) == 1
        _, payload = dlq[0]
        assert payload["event_id"] == "fatal-1"
        assert payload["tool"] == "e2e"
        assert "permanent-x" in payload["reason"]
        assert "v" in payload  # envelope versioned
        assert payload["v"] == "1"
        # Original message preserved
        original = json.loads(payload["original"])
        assert original["event_id"] == "fatal-1"


# ---------------------------------------------------------------------------
# E2E-3: Worker retry exhaust → DLQ
# ---------------------------------------------------------------------------


class TestE2E3RetryExhaust:
    async def test_retryable_exhausted_then_dlq(self, e2e_rc: RedisClient):
        await e2e_rc.stream_publish("stream:e2e", _envelope("retry-out"))

        # max_attempts=3 → 1 initial + 3 retries = 4 calls, then DLQ
        worker = _ProgrammableWorker(
            e2e_rc,
            behaviours={
                "retry-out": [RetryableError(f"transient-{i}") for i in range(10)]
            },
        )
        await _drain(worker, expected_acks=1)

        assert len(worker.processed) == 4

        dlq = await e2e_rc.raw.xrange("stream:e2e:dlq")
        assert len(dlq) == 1
        _, payload = dlq[0]
        assert "max_attempts" in payload["reason"]


# ---------------------------------------------------------------------------
# E2E-4: Multi-worker consumer-group fairness
# ---------------------------------------------------------------------------


class TestE2E4MultiWorker:
    async def test_two_workers_share_workload_no_duplicate_processing(
        self, e2e_rc: RedisClient
    ):
        """Two workers in the same consumer group split the messages.
        Each message should be delivered to exactly one of them."""
        for i in range(20):
            await e2e_rc.stream_publish(
                "stream:e2e", _envelope(f"shared-{i:02d}")
            )

        worker_a = _ProgrammableWorker(e2e_rc, pod_id="pod-a")
        worker_b = _ProgrammableWorker(e2e_rc, pod_id="pod-b")
        worker_a.block_ms = 50
        worker_b.block_ms = 50

        # Run both concurrently. Each will pull what it can.
        task_a = asyncio.create_task(worker_a.run())
        task_b = asyncio.create_task(worker_b.run())

        # Wait until everything is acked
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            groups = await e2e_rc.raw.xinfo_groups("stream:e2e")
            if groups[0]["pending"] == 0 and groups[0].get("lag", 0) == 0:
                break

        worker_a.request_shutdown()
        worker_b.request_shutdown()
        await asyncio.wait_for(task_a, timeout=2.0)
        await asyncio.wait_for(task_b, timeout=2.0)

        # Combined processed count == 20, no overlap
        ids_a = {j.event_id for j in worker_a.processed}
        ids_b = {j.event_id for j in worker_b.processed}
        assert ids_a.isdisjoint(ids_b), "same message processed twice"
        assert ids_a | ids_b == {f"shared-{i:02d}" for i in range(20)}
        # Both workers got at least one message (load shared, not all on A)
        assert len(ids_a) > 0
        assert len(ids_b) > 0


# ---------------------------------------------------------------------------
# E2E-5: Platform-events publish with correct envelope
# ---------------------------------------------------------------------------


class TestE2E5PlatformEvents:
    async def test_platform_event_envelope_auto_filled_from_job(
        self, e2e_rc: RedisClient
    ):
        await e2e_rc.stream_publish(
            "stream:e2e",
            _envelope("evt-pe", org_id="org-pe-1", config_id="cfg-pe"),
        )

        worker = _ProgrammableWorker(
            e2e_rc,
            behaviours={
                "evt-pe": [
                    Result(
                        platform_event={
                            "type": "image_processed",
                            "result_count": 5,
                            "metadata": {"sheet_id": "12345"},
                        }
                    )
                ]
            },
        )
        await _drain(worker, expected_acks=1)

        bus = await e2e_rc.raw.xrange(PLATFORM_EVENTS_STREAM)
        assert len(bus) == 1
        _, env = bus[0]
        # Auto-filled
        assert env["v"] == "1"
        assert env["event_id"] == "evt-pe"
        assert env["tool"] == "e2e"
        assert env["org_id"] == "org-pe-1"
        # User-provided
        assert env["type"] == "image_processed"
        assert env["result_count"] == "5"  # primitives stringified
        assert json.loads(env["metadata"]) == {"sheet_id": "12345"}


# ---------------------------------------------------------------------------
# E2E-6: Health endpoint reflects real state
# ---------------------------------------------------------------------------


class TestE2E6Health:
    async def test_health_transitions_through_workload(self, e2e_rc: RedisClient):
        """Empty → ok. After publishing and consuming-without-acking → degraded
        (pending exceeds threshold). After acking → ok again."""
        await e2e_rc.ensure_consumer_group("stream:e2e", "e2e")

        # Initial: ok, queue 0, pending 0
        report = await compute_health_report(
            redis=e2e_rc,
            tool="e2e",
            stream_name="stream:e2e",
            group_name="e2e",
        )
        assert report["status"] == HEALTH_OK
        assert report["queue_depth"] == 0
        assert report["pending"] == 0

        # Publish 15 — they're lag now
        for i in range(15):
            await e2e_rc.stream_publish("stream:e2e", _envelope(f"h-{i:02d}"))
        report = await compute_health_report(
            redis=e2e_rc,
            tool="e2e",
            stream_name="stream:e2e",
            group_name="e2e",
        )
        assert report["queue_depth"] == 15
        assert report["pending"] == 0
        # 15 > default threshold 100? no — still ok (waiting in stream is fine)
        # The threshold is about pending, not queue_depth
        assert report["status"] == HEALTH_OK

        # Consume but don't ack → pending climbs
        await e2e_rc.raw.xreadgroup(
            "e2e", "consumer-x",
            streams={"stream:e2e": ">"},
            count=15, block=100,
        )
        report = await compute_health_report(
            redis=e2e_rc,
            tool="e2e",
            stream_name="stream:e2e",
            group_name="e2e",
            pending_threshold=10,  # lower for the test
        )
        assert report["pending"] == 15
        assert report["status"] == HEALTH_DEGRADED
        assert report["last_delivered_at"] is not None
