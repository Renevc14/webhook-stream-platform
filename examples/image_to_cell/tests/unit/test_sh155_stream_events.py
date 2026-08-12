"""SH-155 — publish structured outcomes to Redis Streams.

Covers the two publish paths and the service itself:

- ``stream:platform-events`` gets an ``image_processed`` envelope for every
  successfully uploaded image (row_processor success path, DLQ retry path).
- ``stream:i2c:dlq`` gets a ``row_failed_permanent`` envelope when a
  Supabase DLQ entry is abandoned (retries exhausted, config gone,
  non-retryable Smartsheet error).
- Both are best-effort: no Redis client or a failed XADD never breaks
  processing, and failures are logged on the error path.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.results import ImageResult
from tests.unit.conftest import make_attachment, make_config


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Service: envelope shape + degraded behaviour
# ---------------------------------------------------------------------------


class TestStreamEventsService:
    @pytest.mark.asyncio
    async def test_image_processed_envelope_and_stream(self):
        from src.services import stream_events

        redis = MagicMock()
        redis.stream_publish = AsyncMock(return_value="1721000000000-0")

        with patch(
            "src.services.redis_client.get_redis_client", return_value=redis
        ):
            msg_id = await stream_events.publish_image_processed(
                org_id="org-1",
                config_id="cfg-1",
                worksheet_id=111,
                row_id=222,
                attachment_id=333,
                column_id=444,
                filename="dog.jpg",
            )

        assert msg_id == "1721000000000-0"
        redis.stream_publish.assert_awaited_once()
        stream, envelope = redis.stream_publish.await_args.args
        assert stream == "stream:platform-events"
        assert envelope["v"] == "1"
        assert envelope["type"] == "image_processed"
        assert envelope["tool"] == "image-to-cell"
        assert envelope["org_id"] == "org-1"
        assert envelope["config_id"] == "cfg-1"
        assert envelope["worksheet_id"] == "111"
        assert envelope["row_id"] == "222"
        assert envelope["attachment_id"] == "333"
        assert envelope["column_id"] == "444"
        assert envelope["filename"] == "dog.jpg"
        assert envelope["event_id"]
        assert envelope["ts"]

    @pytest.mark.asyncio
    async def test_dlq_abandoned_envelope_and_stream(self):
        from src.services import stream_events

        redis = MagicMock()
        redis.stream_publish = AsyncMock(return_value="1721000000001-0")

        with patch(
            "src.services.redis_client.get_redis_client", return_value=redis
        ):
            msg_id = await stream_events.publish_dlq_abandoned(
                org_id="org-1",
                worksheet_id=111,
                config_id="cfg-1",
                event_type="attachment_created",
                reason="max retries exhausted",
                dlq_id="dlq-uuid-1",
                row_id=222,
                error_type="SmartsheetError",
                retry_count=3,
            )

        assert msg_id == "1721000000001-0"
        stream, envelope = redis.stream_publish.await_args.args
        assert stream == "stream:i2c:dlq"
        assert envelope["v"] == "1"
        assert envelope["type"] == "row_failed_permanent"
        assert envelope["event_id"] == "dlq-uuid-1"
        assert envelope["org_id"] == "org-1"
        assert envelope["worksheet_id"] == "111"
        assert envelope["config_id"] == "cfg-1"
        assert envelope["row_id"] == "222"
        assert envelope["source_event_type"] == "attachment_created"
        assert envelope["reason"] == "max retries exhausted"
        assert envelope["error_type"] == "SmartsheetError"
        assert envelope["retry_count"] == "3"
        assert envelope["dlq_table"] == "imagetocell_dlq"

    @pytest.mark.asyncio
    async def test_no_redis_client_is_a_silent_noop(self):
        """Unit tests / degraded startup have no client wired — the publish
        must not raise and must return None."""
        from src.services import stream_events

        with patch(
            "src.services.redis_client.get_redis_client", return_value=None
        ):
            msg_id = await stream_events.publish_image_processed(
                org_id="org-1",
                config_id="cfg-1",
                worksheet_id=1,
                row_id=2,
                attachment_id=3,
            )

        assert msg_id is None

    @pytest.mark.asyncio
    async def test_failed_xadd_logs_error_and_returns_none(self, caplog):
        """stream_publish returns None on RedisError — the ticket requires a
        log on every publish failure."""
        from src.services import stream_events

        redis = MagicMock()
        redis.stream_publish = AsyncMock(return_value=None)

        with patch(
            "src.services.redis_client.get_redis_client", return_value=redis
        ), caplog.at_level(logging.ERROR, logger="src.services.stream_events"):
            msg_id = await stream_events.publish_dlq_abandoned(
                org_id="org-1",
                worksheet_id=1,
                config_id=None,
                event_type="attachment_created",
                reason="boom",
            )

        assert msg_id is None
        assert any("publish to stream:i2c:dlq failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reason_is_truncated(self):
        from src.services import stream_events

        redis = MagicMock()
        redis.stream_publish = AsyncMock(return_value="1-0")

        with patch(
            "src.services.redis_client.get_redis_client", return_value=redis
        ):
            await stream_events.publish_dlq_abandoned(
                org_id="org-1",
                worksheet_id=1,
                config_id=None,
                event_type="attachment_created",
                reason="x" * 5000,
            )

        _, envelope = redis.stream_publish.await_args.args
        assert len(envelope["reason"]) == 1000


# ---------------------------------------------------------------------------
# Success path: row_processor publishes image_processed per successful image
# ---------------------------------------------------------------------------


def _patch_bolts(monkeypatch):
    """No-op kept for call sites; billing is injected via MockBilling."""
    return


def _make_row_processor(image_results, billing=None):
    from smarthub_core.billing import MockBilling
    from src.core.row_processor import RowProcessor

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(
        MagicMock(),
        MagicMock(),
        settings,
        billing=billing or MockBilling(),
    )
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(return_value=image_results)
    return rp


class TestRowProcessorPublishesOnSuccess:
    def test_success_emits_platform_event(self, monkeypatch):
        _patch_bolts(monkeypatch)
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.core.row_processor.publish_image_processed", _fake_publish
        )
        monkeypatch.setattr(
            "src.core.row_processor.log_usage", lambda **kwargs: "fake-uuid"
        )

        cfg = make_config(
            config_type="default", n_image_columns=1, images_to_process=1
        )
        att = make_attachment(id=77, name="cat.jpg")
        row = {"id": 999, "attachments": [att], "cells": []}

        rp = _make_row_processor(
            [
                ImageResult(
                    success=True,
                    attachment_id=att.id,
                    filename=att.name,
                    column_id=cfg.image_columns[0],
                    original_size_bytes=2048,
                    processing_time_ms=40,
                )
            ]
        )
        rp._sheet_service.extract_row_attachments.return_value = [att]

        result = _run(rp.process(cfg, row, is_scan_context=False))

        assert result.success is True
        assert len(publishes) == 1
        evt = publishes[0]
        assert evt["org_id"] == cfg.organization_id
        assert evt["config_id"] == cfg.id
        assert evt["worksheet_id"] == cfg.worksheet_id
        assert evt["row_id"] == 999
        assert evt["attachment_id"] == att.id
        assert evt["filename"] == att.name

    def test_failed_upload_does_not_publish(self, monkeypatch):
        _patch_bolts(monkeypatch)
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.core.row_processor.publish_image_processed", _fake_publish
        )
        monkeypatch.setattr(
            "src.core.row_processor.log_usage", lambda **kwargs: "fake-uuid"
        )

        cfg = make_config(
            config_type="default", n_image_columns=1, images_to_process=1
        )
        att = make_attachment(id=78, name="broken.jpg")
        row = {"id": 999, "attachments": [att], "cells": []}

        rp = _make_row_processor(
            [
                ImageResult(
                    success=False,
                    attachment_id=att.id,
                    filename=att.name,
                    error="upload failed",
                )
            ]
        )
        rp._sheet_service.extract_row_attachments.return_value = [att]

        result = _run(rp.process(cfg, row, is_scan_context=False))

        assert result.images_failed == 1
        assert publishes == []

    def test_skip_usage_logging_also_skips_publish(self, monkeypatch):
        """DLQ retries call process with skip_usage_logging=True and publish
        from the DLQ processor instead — no double event."""
        _patch_bolts(monkeypatch)
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.core.row_processor.publish_image_processed", _fake_publish
        )
        monkeypatch.setattr(
            "src.core.row_processor.log_usage", lambda **kwargs: "fake-uuid"
        )

        cfg = make_config(
            config_type="default", n_image_columns=1, images_to_process=1
        )
        att = make_attachment(id=79, name="retry.jpg")
        row = {"id": 999, "attachments": [att], "cells": []}

        rp = _make_row_processor(
            [
                ImageResult(
                    success=True,
                    attachment_id=att.id,
                    filename=att.name,
                    column_id=cfg.image_columns[0],
                )
            ]
        )
        rp._sheet_service.extract_row_attachments.return_value = [att]

        result = _run(
            rp.process(cfg, row, is_scan_context=False, skip_usage_logging=True)
        )

        assert result.success is True
        assert publishes == []


# ---------------------------------------------------------------------------
# DLQ path: processor publishes row_failed_permanent on abandonment
# ---------------------------------------------------------------------------


def _make_dlq_processor():
    from src.workers.dlq_processor import DLQProcessor

    proc = DLQProcessor.__new__(DLQProcessor)
    proc._batch_size = 20
    proc._dlq_repo = MagicMock(name="DLQRepo")
    proc._config_repo = MagicMock(name="ConfigRepo")
    proc._processor = MagicMock(name="ImageProcessor")
    return proc


def _make_failed_event(retry_count=2, max_retries=3):
    from src.services.supabase.dlq_repo import FailedEvent

    return FailedEvent(
        id="dlq-1",
        organization_id="org-1",
        worksheet_id=111,
        config_id="cfg-1",
        event_type="attachment_created",
        event_data={"row_id": 222, "attachment_ids": [333]},
        error_message="original failure",
        error_type="SmartsheetError",
        retry_count=retry_count,
        max_retries=max_retries,
    )


class TestDLQProcessorPublishesAbandoned:
    @pytest.mark.asyncio
    async def test_retries_exhausted_publishes_to_dlq_stream(self, monkeypatch):
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event(retry_count=2, max_retries=3)
        proc._dlq_repo.get_pending_events.return_value = [event]
        proc._retry_event = AsyncMock(return_value=False)  # retry fails again

        result = await proc.process_batch()

        assert result.events_abandoned == 1
        assert len(publishes) == 1
        evt = publishes[0]
        assert evt["org_id"] == "org-1"
        assert evt["worksheet_id"] == 111
        assert evt["config_id"] == "cfg-1"
        assert evt["dlq_id"] == "dlq-1"
        assert evt["row_id"] == 222
        assert evt["error_type"] == "SmartsheetError"

    @pytest.mark.asyncio
    async def test_failed_but_retries_left_does_not_publish(self, monkeypatch):
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event(retry_count=0, max_retries=3)
        proc._dlq_repo.get_pending_events.return_value = [event]
        proc._retry_event = AsyncMock(return_value=False)

        result = await proc.process_batch()

        assert result.events_failed == 1
        assert publishes == []

    @pytest.mark.asyncio
    async def test_resolved_does_not_publish(self, monkeypatch):
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event()
        proc._dlq_repo.get_pending_events.return_value = [event]
        proc._retry_event = AsyncMock(return_value=True)

        result = await proc.process_batch()

        assert result.events_resolved == 1
        assert publishes == []

    @pytest.mark.asyncio
    async def test_crash_on_last_retry_publishes(self, monkeypatch):
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event(retry_count=2, max_retries=3)
        proc._dlq_repo.get_pending_events.return_value = [event]
        proc._retry_event = AsyncMock(side_effect=RuntimeError("boom"))

        await proc.process_batch()

        assert len(publishes) == 1
        assert publishes[0]["reason"] == "boom"

    @pytest.mark.asyncio
    async def test_config_gone_abandonment_publishes(self, monkeypatch):
        """_retry_event abandons when the config was deleted and no active
        config exists on the worksheet — that permanent failure must also
        reach the stream."""
        publishes: list = []

        async def _fake_publish(**kwargs):
            publishes.append(kwargs)
            return "1-0"

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event()
        proc._config_repo.get_by_id.return_value = None
        proc._config_repo.get_by_worksheet.return_value = []

        outcome = await proc._retry_event(event)

        assert outcome is True  # abandoned counts as handled
        proc._dlq_repo.mark_as_abandoned.assert_called_once()
        assert len(publishes) == 1
        assert "Config not found" in publishes[0]["reason"]

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_break_abandonment(self, monkeypatch):
        """Redis down while abandoning — the Supabase DLQ flow must finish
        normally (stream is additive, never load-bearing)."""

        async def _fake_publish(**kwargs):
            return None  # XADD failed (already logged by the service)

        monkeypatch.setattr(
            "src.workers.dlq_processor.publish_dlq_abandoned", _fake_publish
        )

        proc = _make_dlq_processor()
        event = _make_failed_event(retry_count=2, max_retries=3)
        proc._dlq_repo.get_pending_events.return_value = [event]
        proc._retry_event = AsyncMock(return_value=False)

        result = await proc.process_batch()

        assert result.events_abandoned == 1
