"""
Integration tests for smarthub_core.worker.base.

Use the real local Redis (via the `rc` fixture) so we exercise XADD →
XREADGROUP → XACK + DLQ + platform-events flows end-to-end. Each test
constructs a small subclass with controllable `process()` behavior.

These tests run against DB 15 with FLUSHDB before each test (see conftest.py).
"""

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
    WorkerError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TEST_TOOL = "tw"  # short test tool slug
TEST_STREAM = f"stream:{TEST_TOOL}"
TEST_GROUP = TEST_TOOL


def _envelope(
    event_id: str = "evt-1",
    tool: str = TEST_TOOL,
    org_id: str = "org-1",
    config_id: str = "cfg-1",
    raw: Optional[dict] = None,
) -> dict[str, str]:
    return {
        "v": "1",
        "event_id": event_id,
        "tool": tool,
        "org_id": org_id,
        "config_id": config_id,
        "received_at": "1700000000",
        "raw": json.dumps(raw or {"k": "v"}),
    }


class _ScriptedWorker(SmarthubWorker):
    """Subclass whose `process()` plays back a list of scripted outcomes.

    Each `outcomes` entry is one of:
      - a Result instance (returned)
      - an Exception (raised)
      - a callable (invoked, return value is processed as above)

    `dedup_key_fn(job)` is called per message; default returns None
    (dedup disabled).
    """

    tool_slug = TEST_TOOL
    base_delay_s = 0.01  # don't wait long during retry tests
    max_attempts = 3

    def __init__(self, redis, *, outcomes, dedup_key_fn=None, **kwargs):
        super().__init__(redis, **kwargs)
        self._outcomes = list(outcomes)
        self._dedup_key_fn = dedup_key_fn
        self.process_calls: list[Job] = []

    def dedup_key(self, job: Job) -> Optional[str]:
        if self._dedup_key_fn is None:
            return None
        return self._dedup_key_fn(job)

    async def process(self, job: Job) -> Result:
        self.process_calls.append(job)
        if not self._outcomes:
            return Result()
        outcome = self._outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(job)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _run_until_drained(worker: SmarthubWorker, *, max_wait_s: float = 2.0) -> None:
    """Run the worker until the stream's pending list is empty for this group,
    then signal shutdown so the run() coroutine returns."""
    task = asyncio.create_task(worker.run())
    deadline = asyncio.get_event_loop().time() + max_wait_s

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        # XINFO GROUPS returns one entry per consumer group with `pending` count
        groups = await worker._redis.raw.xinfo_groups(worker.stream_name)  # type: ignore[attr-defined]
        for g in groups:
            if g["name"] == worker.group_name and g["pending"] == 0:
                # Also need to make sure all messages have been consumed
                stream_info = await worker._redis.raw.xinfo_stream(worker.stream_name)  # type: ignore[attr-defined]
                if stream_info["length"] >= 0:
                    # Check that lag is also 0 — i.e., no messages waiting in stream that the group hasn't read
                    if g.get("lag", 0) == 0:
                        worker.request_shutdown()
                        try:
                            await asyncio.wait_for(task, timeout=worker.block_ms / 1000 + 0.5)
                        except asyncio.TimeoutError:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        return
    # Timeout — cancel and raise
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise AssertionError(
        f"worker did not drain within {max_wait_s}s — calls={len(worker.process_calls)}"  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    async def test_subclass_must_set_tool_slug(self, rc: RedisClient):
        class NoSlug(SmarthubWorker):
            async def process(self, job):
                return Result()

        with pytest.raises(WorkerError, match="tool_slug"):
            NoSlug(rc)

    async def test_consumer_name_format(self, rc: RedisClient):
        w = _ScriptedWorker(rc, outcomes=[], pod_id="pod-abc")
        assert w.consumer_name == f"{TEST_TOOL}-pod-abc"
        assert w.stream_name == TEST_STREAM
        assert w.group_name == TEST_GROUP
        assert w.dlq_stream_name == f"{TEST_STREAM}:dlq"

    async def test_pod_id_defaults_to_uuid(self, rc: RedisClient):
        w = _ScriptedWorker(rc, outcomes=[])
        # uuid4 hex-with-dashes is 36 chars; the consumer is f"{slug}-{uuid}"
        prefix = f"{TEST_TOOL}-"
        assert w.consumer_name.startswith(prefix)
        suffix = w.consumer_name[len(prefix):]
        # parse — will raise if not a valid UUID
        uuid.UUID(suffix)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_single_message_processed_and_acked(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-A"))

        w = _ScriptedWorker(rc, outcomes=[Result()])
        await _run_until_drained(w)

        assert len(w.process_calls) == 1
        assert w.process_calls[0].event_id == "evt-A"
        assert w.process_calls[0].tool == TEST_TOOL
        assert w.process_calls[0].org_id == "org-1"
        assert w.process_calls[0].config_id == "cfg-1"
        assert w.process_calls[0].raw == {"k": "v"}

        # No message left pending
        groups = await rc.raw.xinfo_groups(TEST_STREAM)
        assert groups[0]["pending"] == 0

    async def test_multiple_messages_processed_in_order(self, rc: RedisClient):
        for i in range(3):
            await rc.stream_publish(TEST_STREAM, _envelope(f"evt-{i}"))

        w = _ScriptedWorker(rc, outcomes=[Result(), Result(), Result()])
        await _run_until_drained(w)

        assert [j.event_id for j in w.process_calls] == ["evt-0", "evt-1", "evt-2"]


# ---------------------------------------------------------------------------
# Dedup gate
# ---------------------------------------------------------------------------


class TestDedupGate:
    async def test_dedup_skips_when_key_already_set(self, rc: RedisClient):
        # Pre-claim the dedup key
        await rc.dedup_set("dedup:tw:cfg-1:row-1", ttl_s=60)

        await rc.stream_publish(TEST_STREAM, _envelope("evt-dup"))

        def keyfn(job):
            return "dedup:tw:cfg-1:row-1"

        w = _ScriptedWorker(rc, outcomes=[Result()], dedup_key_fn=keyfn)
        await _run_until_drained(w)

        # process() should NOT be called — duplicate skipped
        assert len(w.process_calls) == 0
        # But message is acked (no pending)
        groups = await rc.raw.xinfo_groups(TEST_STREAM)
        assert groups[0]["pending"] == 0

    async def test_dedup_first_message_claims_then_second_skipped(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-A"))
        await rc.stream_publish(TEST_STREAM, _envelope("evt-B"))

        def keyfn(job):
            return "dedup:tw:cfg-1:row-shared"  # same key for both

        w = _ScriptedWorker(rc, outcomes=[Result(), Result()], dedup_key_fn=keyfn)
        await _run_until_drained(w)

        assert len(w.process_calls) == 1  # only first claimed; second deduped
        assert w.process_calls[0].event_id == "evt-A"


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


class TestRetry:
    async def test_retryable_then_success(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-R"))

        w = _ScriptedWorker(
            rc,
            outcomes=[
                RetryableError("transient #1"),
                RetryableError("transient #2"),
                Result(),  # 3rd attempt succeeds
            ],
        )
        await _run_until_drained(w)

        assert len(w.process_calls) == 3
        # No DLQ entry
        dlq_info = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_info == 0

    async def test_retryable_exhausted_goes_to_dlq(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-X"))

        # max_attempts=3 with all retryable → 4 total calls (1 initial + 3 retries),
        # then DLQ
        w = _ScriptedWorker(
            rc,
            outcomes=[RetryableError("always") for _ in range(10)],
        )
        await _run_until_drained(w)

        # 1 initial + 3 retries = 4 calls, then exhausted
        assert len(w.process_calls) == 4

        # Message went to DLQ
        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1

        # DLQ entry contains the reason and event_id
        dlq_messages = await rc.raw.xrange(w.dlq_stream_name)
        _, dlq_payload = dlq_messages[0]
        assert dlq_payload["event_id"] == "evt-X"
        assert "max_attempts" in dlq_payload["reason"]
        assert dlq_payload["tool"] == TEST_TOOL

    async def test_fatal_goes_straight_to_dlq(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-F"))

        w = _ScriptedWorker(
            rc,
            outcomes=[FatalError("permanent failure")],
        )
        await _run_until_drained(w)

        assert len(w.process_calls) == 1  # NO retry on FatalError

        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1
        _, payload = (await rc.raw.xrange(w.dlq_stream_name))[0]
        assert payload["event_id"] == "evt-F"
        assert "permanent failure" in payload["reason"]

    async def test_unexpected_exception_goes_to_dlq(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-U"))

        w = _ScriptedWorker(
            rc,
            outcomes=[ValueError("unexpected programming error")],
        )
        await _run_until_drained(w)

        assert len(w.process_calls) == 1
        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1
        _, payload = (await rc.raw.xrange(w.dlq_stream_name))[0]
        assert "unexpected" in payload["reason"]
        assert "ValueError" in payload["reason"]


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


class TestEnvelopeParsing:
    async def test_missing_envelope_version_goes_to_dlq(self, rc: RedisClient):
        bad = {"event_id": "evt-bad", "tool": TEST_TOOL, "raw": "{}"}
        await rc.stream_publish(TEST_STREAM, bad)

        w = _ScriptedWorker(rc, outcomes=[])
        await _run_until_drained(w)

        # process() never called
        assert len(w.process_calls) == 0
        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1
        _, payload = (await rc.raw.xrange(w.dlq_stream_name))[0]
        assert "envelope_parse_failed" in payload["reason"]

    async def test_wrong_envelope_version_goes_to_dlq(self, rc: RedisClient):
        bad = _envelope("evt-bad")
        bad["v"] = "99"
        await rc.stream_publish(TEST_STREAM, bad)

        w = _ScriptedWorker(rc, outcomes=[])
        await _run_until_drained(w)

        assert len(w.process_calls) == 0
        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1

    async def test_missing_event_id_goes_to_dlq(self, rc: RedisClient):
        bad = _envelope()
        del bad["event_id"]
        await rc.stream_publish(TEST_STREAM, bad)

        w = _ScriptedWorker(rc, outcomes=[])
        await _run_until_drained(w)

        assert len(w.process_calls) == 0
        dlq_len = await rc.raw.xlen(w.dlq_stream_name)
        assert dlq_len == 1

    async def test_unparseable_raw_passes_through_as_string(self, rc: RedisClient):
        env = _envelope("evt-raw-string")
        env["raw"] = "not-valid-json-but-thats-fine"
        await rc.stream_publish(TEST_STREAM, env)

        w = _ScriptedWorker(rc, outcomes=[Result()])
        await _run_until_drained(w)

        assert len(w.process_calls) == 1
        assert w.process_calls[0].raw == "not-valid-json-but-thats-fine"


# ---------------------------------------------------------------------------
# Platform-events publishing
# ---------------------------------------------------------------------------


class TestPlatformEventsPublishing:
    async def test_result_with_platform_event_publishes_to_bus(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-pub"))

        w = _ScriptedWorker(
            rc,
            outcomes=[
                Result(
                    platform_event={
                        "type": "image_processed",
                        "extra": {"foo": "bar"},
                    }
                )
            ],
        )
        await _run_until_drained(w)

        bus_len = await rc.raw.xlen(PLATFORM_EVENTS_STREAM)
        assert bus_len == 1
        msgs = await rc.raw.xrange(PLATFORM_EVENTS_STREAM)
        _, payload = msgs[0]
        # The envelope auto-fills v, event_id, tool, org_id from the job
        assert payload["v"] == "1"
        assert payload["event_id"] == "evt-pub"
        assert payload["tool"] == TEST_TOOL
        assert payload["org_id"] == "org-1"
        assert payload["type"] == "image_processed"
        # Nested dicts are JSON-encoded into the string field
        assert json.loads(payload["extra"]) == {"foo": "bar"}

    async def test_result_without_platform_event_does_not_publish(self, rc: RedisClient):
        await rc.stream_publish(TEST_STREAM, _envelope("evt-nopub"))

        w = _ScriptedWorker(rc, outcomes=[Result()])
        await _run_until_drained(w)

        bus_len = await rc.raw.xlen(PLATFORM_EVENTS_STREAM)
        assert bus_len == 0

    async def test_ack_happens_before_platform_event_publish(self, rc: RedisClient):
        """Order matters: TARGET_ARCHITECTURE.md §Database update sequence
        Escenario 4 specifies ACK before publish so worker death between ack
        and publish loses only the platform-event (which is best-effort).

        We can't directly observe the order, but we can verify the final state
        is consistent: ack happened (no pending) AND event published."""
        await rc.stream_publish(TEST_STREAM, _envelope("evt-order"))

        w = _ScriptedWorker(
            rc,
            outcomes=[Result(platform_event={"type": "test"})],
        )
        await _run_until_drained(w)

        groups = await rc.raw.xinfo_groups(TEST_STREAM)
        assert groups[0]["pending"] == 0  # ACKed

        bus_len = await rc.raw.xlen(PLATFORM_EVENTS_STREAM)
        assert bus_len == 1


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    async def test_request_shutdown_stops_loop(self, rc: RedisClient):
        # Start with no messages. Use a very short block_ms so the read loop
        # returns quickly between iterations and shutdown is observed within
        # ~50ms after request_shutdown(). Generous timeout absorbs occasional
        # Windows redis-socket tail latency.
        w = _ScriptedWorker(rc, outcomes=[])
        w.block_ms = 50

        task = asyncio.create_task(w.run())
        # Let the loop spin up and start blocking on xreadgroup
        await asyncio.sleep(0.05)

        w.request_shutdown()

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pytest.fail("worker did not shut down within 5s after request_shutdown()")
