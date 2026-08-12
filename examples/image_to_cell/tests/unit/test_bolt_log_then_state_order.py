"""
Tests for B15 — log_usage runs before state_repo.save in RowProcessor.process.

Why the order matters:
    Old order (state then log): if state_repo.save succeeded but log_usage
    failed, the row had state but no billing record → subcobro.

    Newer concern (the customer-reported bug): if state_repo.save FAILED
    after a successful upload, no state and no log were written. The next
    backfill saw no row_state → built synthetic state from usage_log →
    found nothing for this row → treated the attachment as new →
    re-uploaded + re-billed. Double charge.

Inverting the order eliminates the double-bill:
    - log_usage succeeds → state.save fails → usage_log has the entry →
      next backfill's synthetic state build sees it → skips the row → 0
      bolts billed again.
    - log_usage fails → state.save still runs → cell filled, no bill →
      subcobro. Acceptable as the lesser evil.

This test family pins the order using a sentinel that records the
sequence of mocked calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.conftest import make_attachment, make_config


def _run(coro):
    """Run an async coroutine to completion inside a synchronous test.

    SH-101: the RowProcessor.process method is now ``async def``; the tests
    in this module exercise call ordering without needing an asyncio event
    loop, so each call is wrapped here rather than converting the whole
    file to ``pytest.mark.asyncio``.
    """
    return asyncio.run(coro)


def _build_processor(state_save_raises: Exception | None = None,
                     log_usage_raises: Exception | None = None):
    """Construct a RowProcessor with mocked external dependencies, and
    return (processor, call_log) where call_log is an ordered list of
    'log_usage' / 'state_save' strings."""
    from smarthub_core.billing import MockBilling
    from src.core.row_processor import RowProcessor

    call_log: list[str] = []

    fake_smartsheet = MagicMock(name="SmartsheetService")
    fake_state_repo = MagicMock(name="StateRepository")

    def fake_state_save(state):
        call_log.append("state_save")
        if state_save_raises is not None:
            raise state_save_raises
        return None

    fake_state_repo.save.side_effect = fake_state_save
    fake_state_repo.get.return_value = None  # no existing state

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False

    rp = RowProcessor(
        fake_smartsheet, fake_state_repo, settings, billing=MockBilling()
    )

    # Replace the actual sheet service and image handler with mocks.
    # SH-101: ImageHandler.process_batch is async, so the test mock must
    # return a coroutine — wrap it with AsyncMock.
    rp._sheet_service = MagicMock(name="SheetService")
    rp._image_handler = MagicMock(name="ImageHandler")
    rp._image_handler.process_batch = AsyncMock()

    return rp, call_log, fake_state_repo


def test_log_usage_runs_before_state_save_on_happy_path(monkeypatch):
    """Verify the order of side-effects when both log_usage and state.save
    succeed. log_usage should be called first."""
    rp, call_log, _ = _build_processor()

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=1, name="img.jpg")
    row = {"id": 999, "attachments": [att]}

    rp._sheet_service.extract_row_attachments.return_value = [att]

    from src.models.results import ImageResult
    image_result = ImageResult(
        success=True,
        attachment_id=att.id,
        filename=att.name,
        column_id=cfg.image_columns[0],
        original_size_bytes=1024,
        processing_time_ms=50,
    )
    rp._image_handler.process_batch = AsyncMock(return_value=[image_result])

    # Patch log_usage at the module level to record order
    def fake_log_usage(**kwargs):
        call_log.append("log_usage")
        return "fake-uuid"

    monkeypatch.setattr("src.core.row_processor.log_usage", fake_log_usage)

    result = _run(rp.process(cfg, row))

    assert result.success is True
    assert call_log == ["log_usage", "state_save"], (
        f"Expected log_usage to run before state_save, got order: {call_log}"
    )


def test_state_save_failure_does_not_skip_log_usage(monkeypatch):
    """If state_repo.save raises AFTER log_usage already ran, the log
    entry persists. The next backfill will see it and skip → no double-
    bill. Verifies the customer-reported overcharge path is closed."""
    save_error = RuntimeError("Server disconnected mid-state-save")
    rp, call_log, fake_state_repo = _build_processor(state_save_raises=save_error)

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=1, name="img.jpg")
    row = {"id": 999, "attachments": [att]}

    rp._sheet_service.extract_row_attachments.return_value = [att]

    from src.models.results import ImageResult
    image_result = ImageResult(
        success=True,
        attachment_id=att.id,
        filename=att.name,
        column_id=cfg.image_columns[0],
        original_size_bytes=1024,
        processing_time_ms=50,
    )
    rp._image_handler.process_batch = AsyncMock(return_value=[image_result])

    def fake_log_usage(**kwargs):
        call_log.append("log_usage")
        return "fake-uuid"

    monkeypatch.setattr("src.core.row_processor.log_usage", fake_log_usage)


    result = _run(rp.process(cfg, row))

    # log_usage ran (billing recorded). state_save raised (not recorded).
    assert "log_usage" in call_log
    assert call_log.index("log_usage") < call_log.index("state_save"), (
        "log_usage MUST run before state_save so a transient state.save "
        "failure does not produce a double-bill on retry"
    )

    # The error from state_save propagates to the outer except block → result.success = False
    assert result.success is False
    assert "Server disconnected" in (result.error or "")


def test_log_usage_failure_does_not_skip_state_save(monkeypatch):
    """The reverse direction: if log_usage raises but state_save would
    succeed, we still save state. The trade-off is subcobro instead of
    double-bill — acceptable.

    Note: log_usage today swallows its own exceptions internally and
    returns None (see src/services/usage_log.py). This test models the
    contract: even if log_usage signals failure, the row processor
    proceeds to state_save and returns success based on image upload
    outcome."""
    rp, call_log, fake_state_repo = _build_processor()

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=1, name="img.jpg")
    row = {"id": 999, "attachments": [att]}

    rp._sheet_service.extract_row_attachments.return_value = [att]

    from src.models.results import ImageResult
    image_result = ImageResult(
        success=True,
        attachment_id=att.id,
        filename=att.name,
        column_id=cfg.image_columns[0],
        original_size_bytes=1024,
        processing_time_ms=50,
    )
    rp._image_handler.process_batch = AsyncMock(return_value=[image_result])

    # log_usage swallows internally and returns None (production behaviour).
    def fake_log_usage(**kwargs):
        call_log.append("log_usage_attempted_failed")
        return None  # swallowed failure

    monkeypatch.setattr("src.core.row_processor.log_usage", fake_log_usage)


    result = _run(rp.process(cfg, row))

    # state_save still ran after the (failed) log attempt
    assert call_log == ["log_usage_attempted_failed", "state_save"]
    assert result.success is True  # image upload was OK
    fake_state_repo.save.assert_called_once()


def test_skip_usage_logging_path_still_saves_state(monkeypatch):
    """DLQ retries pass skip_usage_logging=True. State must still be
    saved so the row dedup tracking continues to work."""
    rp, call_log, fake_state_repo = _build_processor()

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=1, name="img.jpg")
    row = {"id": 999, "attachments": [att]}

    rp._sheet_service.extract_row_attachments.return_value = [att]

    from src.models.results import ImageResult
    image_result = ImageResult(
        success=True,
        attachment_id=att.id,
        filename=att.name,
        column_id=cfg.image_columns[0],
        original_size_bytes=1024,
        processing_time_ms=50,
    )
    rp._image_handler.process_batch = AsyncMock(return_value=[image_result])

    def fake_log_usage(**kwargs):
        call_log.append("log_usage")  # should NOT be called

    monkeypatch.setattr("src.core.row_processor.log_usage", fake_log_usage)


    result = _run(rp.process(cfg, row, skip_usage_logging=True))

    assert "log_usage" not in call_log
    assert "state_save" in call_log
    assert result.success is True
