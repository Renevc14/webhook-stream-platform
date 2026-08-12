"""SH-152 — embedded stream consumer for stream:i2c.

Covers the ``I2CStreamWorker`` contract:

- envelope raw → WebhookPayload → existing WebhookHandler pipeline
- provider filter (only ``smartsheet`` processed)
- malformed raw → FatalError → stream DLQ + ack (via SmarthubWorker)
- handler-level failure → RetryableError (backoff, DLQ after max attempts)
- ack ordering: ACK after successful process
- envelope-level dedup key
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from smarthub_core.worker.base import FatalError, Job, RetryableError

from src.models.results import WebhookResult
from src.workers.stream_worker import I2CStreamWorker, CONSUMER_GROUP, STREAM_NAME


SMARTSHEET_CALLBACK = {
    "webhookId": 3285357287499652,
    "scope": "sheet",
    "scopeObjectId": 5830930674876292,
    "events": [
        {
            "objectType": "attachment",
            "eventType": "created",
            "id": 7594242852104068,
            "timestamp": "2026-07-15T18:00:00.000+00:00",
        }
    ],
}


def _make_redis():
    redis = MagicMock(name="RedisClient")
    redis.ensure_consumer_group = AsyncMock()
    redis.stream_ack = AsyncMock(return_value=1)
    redis.stream_publish = AsyncMock(return_value="1-0")
    redis.dedup_set = AsyncMock(return_value=True)
    return redis


def _make_worker(handler_result=None, redis=None):
    handler = MagicMock(name="WebhookHandler")
    handler.handle_webhook = AsyncMock(
        return_value=handler_result
        or WebhookResult(success=True, status="ok", events_processed=1)
    )
    worker = I2CStreamWorker(redis or _make_redis(), webhook_handler=handler)
    return worker, handler


def _make_job(raw, provider="smartsheet", event_id="evt-1"):
    return Job(
        msg_id="1-0",
        event_id=event_id,
        tool="i2c",
        org_id="org-1",
        received_at="2026-07-15T18:00:01+00:00",
        raw=raw,
        payload={"provider": provider, "event_id": event_id},
    )


class TestWorkerIdentity:
    def test_stream_and_group_names(self):
        worker, _ = _make_worker()
        assert worker.stream_name == STREAM_NAME == "stream:i2c"
        assert worker.group_name == CONSUMER_GROUP == "i2c-workers"
        assert worker.dlq_stream_name == "stream:i2c:dlq"

    def test_dedup_key_is_envelope_scoped(self):
        worker, _ = _make_worker()
        job = _make_job(SMARTSHEET_CALLBACK, event_id="abc-123")
        assert worker.dedup_key(job) == "dedup:i2c:envelope:abc-123"


class TestProcess:
    @pytest.mark.asyncio
    async def test_dict_raw_reaches_handler_as_webhook_payload(self):
        worker, handler = _make_worker()
        job = _make_job(SMARTSHEET_CALLBACK)

        result = await worker.process(job)

        assert result.success is True
        handler.handle_webhook.assert_awaited_once()
        payload = handler.handle_webhook.await_args.args[0]
        assert payload.worksheet_id == 5830930674876292
        assert payload.webhook_id == 3285357287499652
        assert len(payload.new_attachment_events) == 1
        assert result.metadata["events_processed"] == 1

    @pytest.mark.asyncio
    async def test_json_string_raw_is_decoded(self):
        worker, handler = _make_worker()
        job = _make_job(json.dumps(SMARTSHEET_CALLBACK))

        result = await worker.process(job)

        assert result.success is True
        payload = handler.handle_webhook.await_args.args[0]
        assert payload.worksheet_id == 5830930674876292

    @pytest.mark.asyncio
    async def test_non_smartsheet_provider_is_skipped(self):
        worker, handler = _make_worker()
        job = _make_job(SMARTSHEET_CALLBACK, provider="hubspot")

        result = await worker.process(job)

        assert result.success is True
        assert result.metadata == {"skipped": "provider"}
        handler.handle_webhook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_raw_string_is_fatal(self):
        worker, handler = _make_worker()
        job = _make_job("this is not json{")

        with pytest.raises(FatalError):
            await worker.process(job)
        handler.handle_webhook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_dict_raw_is_fatal(self):
        worker, _ = _make_worker()
        job = _make_job([1, 2, 3])

        with pytest.raises(FatalError):
            await worker.process(job)

    @pytest.mark.asyncio
    async def test_handler_failure_is_retryable(self):
        worker, _ = _make_worker(
            handler_result=WebhookResult(
                success=False, status="error", message="supabase down"
            )
        )
        job = _make_job(SMARTSHEET_CALLBACK)

        with pytest.raises(RetryableError, match="supabase down"):
            await worker.process(job)

    @pytest.mark.asyncio
    async def test_partial_row_failures_still_ack(self):
        """Handler-level success with failed rows: the handler already stored
        those rows in the Supabase DLQ — the stream message must be acked,
        not retried (rows that succeeded would double-process)."""
        worker, _ = _make_worker(
            handler_result=WebhookResult(
                success=True, status="ok", events_processed=2, events_failed=1
            )
        )
        job = _make_job(SMARTSHEET_CALLBACK)

        result = await worker.process(job)

        assert result.success is True
        assert result.metadata["events_failed"] == 1


class TestHandleMessageFlow:
    """Drive SmarthubWorker._handle_message to pin ack/DLQ ordering."""

    def _payload(self, raw=None, provider="smartsheet"):
        return {
            "v": "1",
            "event_id": "evt-1",
            "tool": "i2c",
            "org_id": "org-1",
            "provider": provider,
            "received_at": "2026-07-15T18:00:01+00:00",
            "raw": json.dumps(raw if raw is not None else SMARTSHEET_CALLBACK),
        }

    @pytest.mark.asyncio
    async def test_success_acks_message(self):
        redis = _make_redis()
        worker, handler = _make_worker(redis=redis)

        await worker._handle_message("1-0", self._payload())

        handler.handle_webhook.assert_awaited_once()
        redis.stream_ack.assert_awaited_once_with(
            "stream:i2c", "i2c-workers", "1-0"
        )
        # No DLQ publish on success.
        redis.stream_publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fatal_raw_goes_to_stream_dlq_and_acks(self):
        redis = _make_redis()
        worker, handler = _make_worker(redis=redis)
        payload = self._payload()
        payload["raw"] = "not-json{"

        await worker._handle_message("1-0", payload)

        handler.handle_webhook.assert_not_awaited()
        redis.stream_publish.assert_awaited_once()
        dlq_stream, dlq_payload = redis.stream_publish.await_args.args
        assert dlq_stream == "stream:i2c:dlq"
        assert dlq_payload["event_id"] == "evt-1"
        redis.stream_ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_envelope_is_skipped_and_acked(self):
        redis = _make_redis()
        redis.dedup_set = AsyncMock(return_value=False)  # someone else has it
        worker, handler = _make_worker(redis=redis)

        await worker._handle_message("1-0", self._payload())

        handler.handle_webhook.assert_not_awaited()
        redis.stream_ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retryable_exhaustion_goes_to_dlq(self):
        redis = _make_redis()
        worker, handler = _make_worker(
            handler_result=WebhookResult(
                success=False, status="error", message="still failing"
            ),
            redis=redis,
        )
        worker.max_attempts = 1
        worker.base_delay_s = 0.0

        await worker._handle_message("1-0", self._payload())

        redis.stream_publish.assert_awaited_once()
        dlq_stream, dlq_payload = redis.stream_publish.await_args.args
        assert dlq_stream == "stream:i2c:dlq"
        assert "still failing" in dlq_payload["reason"]
        redis.stream_ack.assert_awaited_once()
