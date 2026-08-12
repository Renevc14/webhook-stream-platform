"""
End-to-end verification of the SH-75 backfill-abort flow.

When _build_synthetic_states_for_backfill raises PriorLogUnavailable,
_backfill_config must:

    1. Catch the exception (do NOT let it propagate up to backfill_worksheet
       and break sibling configs).
    2. Set BackfillResult.success = False.
    3. Set BackfillResult.error to a message that explicitly mentions
       prior_log_unavailable so the user-facing UI can show the right copy.
    4. Set rows_skipped = len(rows) — every row was deferred.
    5. Return WITHOUT entering the ThreadPoolExecutor (no row work attempted,
       no bolts charged).

This pins the runtime contract that prevents the customer-reported
overcharge from happening even if a future change re-introduces silent
fail-open in the synthetic-state code path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


@pytest.fixture
def fake_smartsheet():
    """Minimal SmartsheetService stand-in. _backfill_config only constructs
    a SheetService and a RowProcessor from it; both go through their own
    mocks below so the actual SmartsheetService implementation is never
    exercised."""
    return MagicMock(name="SmartsheetService")


def test_backfill_config_aborts_when_prior_log_unavailable(
    processor, mock_supabase, fake_smartsheet
):
    """When the prior-log query fails after retries, the backfill returns
    a BackfillResult that the caller (backfill_worksheet) can merge into
    the parent result without breaking sibling configs."""
    cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
    rows = [
        make_row(row_id=i, attachments=[make_attachment(id=i, name=f"img{i}.jpg")])
        for i in range(1, 21)
    ]

    # The synthetic-state build path queries imagetocell_usage_log via
    # i2c_bolt_gate._fetch_prior_processed; configure the mock to raise
    # so retries exhaust and PriorLogUnavailable propagates.
    mock_supabase.usage_log_error = RuntimeError("Server disconnected")

    # state_repo.get_batch returns nothing — every row is "without state",
    # so synthetic state build runs (and raises).
    processor._state_repo.get_batch = MagicMock(return_value={})

    # SH-101: the abort path must not enter the parallel-row processing
    # loop. ThreadPoolExecutor was replaced with asyncio.gather + a
    # row_processor.process await — patch the latter so a stray call would
    # fail loud.
    with patch("src.core.processor.RowProcessor") as MockRowProcessor:
        from unittest.mock import AsyncMock
        bad_call = AsyncMock(
            side_effect=AssertionError(
                "RowProcessor.process called — abort path failed"
            )
        )
        MockRowProcessor.return_value.process = bad_call
        import asyncio
        result = asyncio.run(processor._backfill_config(cfg, rows, fake_smartsheet))

    assert result.success is False
    assert "prior_log_unavailable" in result.error
    assert "retry" in result.error.lower()
    assert result.rows_skipped == len(rows)
    assert result.rows_processed == 0
    assert result.rows_failed == 0


def test_backfill_config_proceeds_normally_when_supabase_ok(
    processor, mock_supabase, fake_smartsheet
):
    """Sanity check — a healthy backfill (no Supabase error) does NOT abort.
    Without this, the abort test could pass for the wrong reason."""
    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    rows = [
        make_row(row_id=1, attachments=[make_attachment(id=1, name="a.jpg")]),
    ]
    mock_supabase.usage_log_rows = []  # healthy, no prior log
    processor._state_repo.get_batch = MagicMock(return_value={})

    # We don't want the actual RowProcessor.process to run (it would try
    # to talk to Smartsheet). Stub it out — any return value is fine
    # because we only assert that we PROCEEDED past the synthetic-state
    # check, not the row-processing outcome.
    with patch("src.core.processor.RowProcessor") as MockRowProcessor:
        instance = MockRowProcessor.return_value
        row_result = MagicMock()
        row_result.images_processed = 0
        # SH-101: RowProcessor.process is async; the stub has to return a
        # coroutine that resolves to ``row_result``.
        from unittest.mock import AsyncMock
        instance.process = AsyncMock(return_value=row_result)

        import asyncio
        result = asyncio.run(processor._backfill_config(cfg, rows, fake_smartsheet))

    # Did NOT abort — error is empty / None, rows_skipped reflects normal
    # flow, not the all-rows-skipped abort signal.
    assert "prior_log_unavailable" not in (result.error or "")
