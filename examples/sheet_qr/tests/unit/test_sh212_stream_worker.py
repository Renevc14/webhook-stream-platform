"""SH-212 — SmartQR stream consumer hardened onto SmarthubWorker."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from smarthub_core.worker.base import ENVELOPE_VERSION, WorkerError

from smart_qr.workers.stream_worker import SmartQRStreamWorker

ORG = str(uuid4())
CONFIG_ID = uuid4()
SHEET_ID = 4242


class FakeRedis:
    """Records ack/DLQ traffic; the worker only uses these three calls."""

    def __init__(self):
        self.acked: list[str] = []
        self.published: list[tuple[str, dict]] = []
        self.dedup_keys: list[str] = []
        self.groups: list[tuple[str, str]] = []

    async def ensure_consumer_group(self, stream, group, **_k):
        self.groups.append((stream, group))
        return True

    async def stream_ack(self, stream, group, *msg_ids):
        self.acked.extend(msg_ids)
        return len(msg_ids)

    async def stream_publish(self, stream, payload):
        self.published.append((stream, payload))
        return "1-1"

    async def dedup_set(self, key, ttl_s):
        self.dedup_keys.append(key)
        return True

    @property
    def dlq(self):
        return [p for s, p in self.published if s.endswith(":dlq")]


class FakeProcessor:
    def __init__(self, fail_times: int = 0):
        self.calls: list[dict] = []
        self._fail_times = fail_times

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("smartsheet 500")
        return SimpleNamespace(status="ok", reason=None)


class FakeConfigs:
    def __init__(self, configs=None, error: Exception | None = None):
        self._configs = configs if configs is not None else [SimpleNamespace(id=CONFIG_ID)]
        self._error = error
        self.calls: list[tuple] = []

    async def list_for_worksheet(self, org_id, worksheet_id):
        self.calls.append((org_id, worksheet_id))
        if self._error:
            raise self._error
        return list(self._configs)


def _worker(redis=None, processor=None, configs=None) -> SmartQRStreamWorker:
    return SmartQRStreamWorker(
        redis or FakeRedis(),
        processor=processor or FakeProcessor(),
        configs=configs or FakeConfigs(),
        pod_id="test-pod",
    )


def _envelope(body: dict, **overrides) -> dict:
    payload = {
        "v": ENVELOPE_VERSION,
        "event_id": "evt-1",
        "provider": "smartsheet",
        "tool": "smart-qr",
        "org_id": ORG,
        "received_at": "2026-07-29T00:00:00Z",
        "raw": json.dumps(body),
    }
    payload.update(overrides)
    return payload


def _callback_body(events: list[dict]) -> dict:
    return {
        "scopeObjectId": SHEET_ID,
        "eventCount": len(events),
        "events": events,
    }


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_worker_uses_shared_stream_and_group_names():
    worker = _worker()
    assert worker.stream_name == "stream:smart-qr"
    assert worker.group_name == "smart-qr-workers"
    assert worker.consumer_name == "smart-qr-test-pod"
    assert worker.dlq_stream_name == "stream:smart-qr:dlq"
    # Orphan recovery must stay on: a crashed pod would otherwise strand
    # every in-flight webhook in the pending list.
    assert worker.claim_idle_ms == 60_000


def test_worker_does_not_dedup_envelopes():
    # A dedup key outliving claim_idle_ms would make reclaimed messages ack
    # without being processed. Row-hash dedup in ProcessRowService covers us.
    worker = _worker()
    job = worker._parse_envelope("1-1", _envelope(_callback_body([])))
    assert worker.dedup_key(job) is None


# ---------------------------------------------------------------------------
# Gateway envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_envelope_fans_events_out_to_configs():
    processor = FakeProcessor()
    redis = FakeRedis()
    worker = _worker(redis, processor)
    body = _callback_body(
        [
            {"objectType": "row", "rowId": 11, "columnId": 99, "eventType": "updated"},
            {"objectType": "row", "rowId": 12, "eventType": "updated"},
        ]
    )

    await worker._handle_message("1-1", _envelope(body))

    assert [c["row_id"] for c in processor.calls] == [11, 12]
    assert processor.calls[0]["changed_column_ids"] == [99]
    assert processor.calls[1]["changed_column_ids"] is None
    assert processor.calls[0]["organization_id"] == UUID(ORG)
    assert processor.calls[0]["config_id"] == CONFIG_ID
    assert redis.acked == ["1-1"]
    assert redis.dlq == []


@pytest.mark.asyncio
async def test_own_change_agent_events_are_skipped():
    processor = FakeProcessor()
    worker = _worker(processor=processor)
    body = _callback_body(
        [{"rowId": 11, "changeAgent": "smart-qr-v1"}, {"rowId": 12}]
    )

    result = await worker.process(worker._parse_envelope("1-1", _envelope(body)))

    assert [c["row_id"] for c in processor.calls] == [12]
    assert result.metadata["self_events_skipped"] == 1


@pytest.mark.asyncio
async def test_envelope_without_scope_object_id_is_skipped():
    processor = FakeProcessor()
    worker = _worker(processor=processor)

    result = await worker.process(
        worker._parse_envelope("1-1", _envelope({"events": [{"rowId": 1}]}))
    )

    assert result.metadata == {"skipped": "no_scope_object_id"}
    assert processor.calls == []


@pytest.mark.asyncio
async def test_sheet_without_config_is_skipped_not_dlqd():
    redis = FakeRedis()
    worker = _worker(redis, configs=FakeConfigs(configs=[]))

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 1}])))

    assert redis.acked == ["1-1"]
    assert redis.dlq == []


@pytest.mark.asyncio
async def test_non_smartsheet_provider_is_skipped():
    processor = FakeProcessor()
    worker = _worker(processor=processor)
    payload = _envelope(_callback_body([{"rowId": 1}]), provider="slack")

    result = await worker.process(worker._parse_envelope("1-1", payload))

    assert result.metadata == {"skipped": "provider"}
    assert processor.calls == []


# ---------------------------------------------------------------------------
# Direct entries (manual XADD used by smoke scripts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_entry_without_envelope_version_still_processes():
    processor = FakeProcessor()
    redis = FakeRedis()
    worker = _worker(redis, processor)

    await worker._handle_message(
        "1-1",
        {"org_id": ORG, "config_id": str(CONFIG_ID), "row_id": "77"},
    )

    assert processor.calls[0]["row_id"] == 77
    assert processor.calls[0]["config_id"] == CONFIG_ID
    assert redis.acked == ["1-1"]


def test_unrecognized_entry_shape_is_rejected():
    worker = _worker()
    with pytest.raises(WorkerError):
        worker._parse_envelope("1-1", {"hello": "world"})


@pytest.mark.asyncio
async def test_garbage_entry_goes_to_dlq_and_is_acked():
    redis = FakeRedis()
    worker = _worker(redis)

    await worker._handle_message("1-1", {"hello": "world"})

    assert redis.acked == ["1-1"]
    assert len(redis.dlq) == 1
    assert "envelope_parse_failed" in redis.dlq[0]["reason"]


@pytest.mark.asyncio
async def test_invalid_raw_json_goes_to_dlq():
    redis = FakeRedis()
    worker = _worker(redis)
    payload = _envelope({})
    payload["raw"] = "{not json"

    await worker._handle_message("1-1", payload)

    assert redis.acked == ["1-1"]
    assert len(redis.dlq) == 1


# ---------------------------------------------------------------------------
# Retry / DLQ semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_row_failure_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(
        "smarthub_core.worker.base.asyncio.sleep", _no_sleep
    )
    processor = FakeProcessor(fail_times=2)
    redis = FakeRedis()
    worker = _worker(redis, processor)

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 11}])))

    assert len(processor.calls) == 3
    assert redis.acked == ["1-1"]
    assert redis.dlq == []


@pytest.mark.asyncio
async def test_permanent_row_failure_lands_in_dlq(monkeypatch):
    monkeypatch.setattr(
        "smarthub_core.worker.base.asyncio.sleep", _no_sleep
    )
    processor = FakeProcessor(fail_times=99)
    redis = FakeRedis()
    worker = _worker(redis, processor)

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 11}])))

    assert redis.acked == ["1-1"]
    assert len(redis.dlq) == 1
    assert "row processing failed" in redis.dlq[0]["reason"]


@pytest.mark.asyncio
async def test_config_lookup_failure_is_retryable(monkeypatch):
    monkeypatch.setattr(
        "smarthub_core.worker.base.asyncio.sleep", _no_sleep
    )
    redis = FakeRedis()
    worker = _worker(redis, configs=FakeConfigs(error=RuntimeError("supabase down")))

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 11}])))

    assert len(redis.dlq) == 1
    assert "config lookup failed" in redis.dlq[0]["reason"]


@pytest.mark.asyncio
async def test_process_result_error_is_retried(monkeypatch):
    """Typed ProcessResult(status=error) must not be silently ACKed."""
    monkeypatch.setattr(
        "smarthub_core.worker.base.asyncio.sleep", _no_sleep
    )

    class ResultErrorProcessor:
        def __init__(self):
            self.calls = 0

        async def process(self, **kwargs):
            self.calls += 1
            if self.calls < 3:
                return SimpleNamespace(status="error", reason="smartsheet timeout")
            return SimpleNamespace(status="ok", reason=None)

    processor = ResultErrorProcessor()
    redis = FakeRedis()
    worker = _worker(redis, processor)

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 11}])))

    assert processor.calls == 3
    assert redis.acked == ["1-1"]
    assert redis.dlq == []


@pytest.mark.asyncio
async def test_process_result_fatal_lands_in_dlq(monkeypatch):
    monkeypatch.setattr(
        "smarthub_core.worker.base.asyncio.sleep", _no_sleep
    )

    class FatalResultProcessor:
        async def process(self, **kwargs):
            return SimpleNamespace(status="error", reason="config_not_found")

    redis = FakeRedis()
    worker = _worker(redis, FatalResultProcessor())

    await worker._handle_message("1-1", _envelope(_callback_body([{"rowId": 11}])))

    assert redis.acked == ["1-1"]
    assert len(redis.dlq) == 1
    assert "config_not_found" in redis.dlq[0]["reason"]


async def _no_sleep(_seconds):
    return None
