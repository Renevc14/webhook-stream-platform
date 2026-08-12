"""SH-212 — opt-in orphan recovery in SmarthubWorker (no live Redis needed)."""

from __future__ import annotations

import json

import pytest

from smarthub_core.worker.base import ENVELOPE_VERSION, Job, Result, SmarthubWorker


class FakeRaw:
    def __init__(self, claim_batches=None):
        self._claim_batches = list(claim_batches or [])
        self.claim_calls: list[dict] = []

    async def xautoclaim(self, stream, group, consumer, **kwargs):
        self.claim_calls.append({"stream": stream, "consumer": consumer, **kwargs})
        if not self._claim_batches:
            return ("0-0", [], [])
        return self._claim_batches.pop(0)

    async def xreadgroup(self, *_a, **_k):
        return []


class FakeRedis:
    def __init__(self, claim_batches=None):
        self.raw = FakeRaw(claim_batches)
        self.acked: list[str] = []
        self.published: list[tuple] = []

    async def ensure_consumer_group(self, stream, group, **_k):
        return True

    async def stream_ack(self, stream, group, *msg_ids):
        self.acked.extend(msg_ids)
        return len(msg_ids)

    async def stream_publish(self, stream, payload):
        self.published.append((stream, payload))
        return "1-1"

    async def dedup_set(self, key, ttl_s):
        return True


class RecordingWorker(SmarthubWorker):
    tool_slug = "demo"
    claim_idle_ms = 30_000

    def __init__(self, redis, **kwargs):
        super().__init__(redis, **kwargs)
        self.handled: list[Job] = []

    async def process(self, job: Job) -> Result:
        self.handled.append(job)
        return Result(success=True)


def _entry(msg_id: str, event_id: str):
    return (
        msg_id,
        {
            "v": ENVELOPE_VERSION,
            "event_id": event_id,
            "tool": "demo",
            "org_id": "org-1",
            "raw": json.dumps({"hello": "world"}),
        },
    )


@pytest.mark.asyncio
async def test_reclaimed_entries_are_processed_and_acked():
    redis = FakeRedis(claim_batches=[("0-0", [_entry("1-1", "evt-a")], [])])
    worker = RecordingWorker(redis, pod_id="pod")

    await worker._reclaim_idle()

    assert [j.event_id for j in worker.handled] == ["evt-a"]
    assert redis.acked == ["1-1"]
    assert redis.raw.claim_calls[0]["min_idle_time"] == 30_000
    assert redis.raw.claim_calls[0]["consumer"] == "demo-pod"


@pytest.mark.asyncio
async def test_dangling_pending_entry_is_acked_without_processing():
    # Redis returns an empty payload when the stream entry was trimmed away.
    redis = FakeRedis(claim_batches=[("0-0", [("1-1", {})], [])])
    worker = RecordingWorker(redis, pod_id="pod")

    await worker._reclaim_idle()

    assert worker.handled == []
    assert redis.acked == ["1-1"]


@pytest.mark.asyncio
async def test_two_tuple_xautoclaim_response_is_supported():
    redis = FakeRedis(claim_batches=[("0-0", [_entry("1-1", "evt-a")])])
    worker = RecordingWorker(redis, pod_id="pod")

    await worker._reclaim_idle()

    assert [j.event_id for j in worker.handled] == ["evt-a"]


@pytest.mark.asyncio
async def test_missing_xautoclaim_degrades_quietly():
    redis = FakeRedis()

    async def boom(*_a, **_k):
        raise RuntimeError("unknown command 'XAUTOCLAIM'")

    redis.raw.xautoclaim = boom  # type: ignore[method-assign]
    worker = RecordingWorker(redis, pod_id="pod")

    await worker._reclaim_idle()

    assert worker.handled == []
    assert redis.acked == []


@pytest.mark.asyncio
async def test_reclaim_is_off_unless_claim_idle_ms_is_set():
    class NoClaimWorker(RecordingWorker):
        claim_idle_ms = 0

    redis = FakeRedis(claim_batches=[("0-0", [_entry("1-1", "evt-a")], [])])
    worker = NoClaimWorker(redis, pod_id="pod")
    worker.request_shutdown()

    await worker.run()

    assert redis.raw.claim_calls == []
    assert worker.is_shutting_down is True
