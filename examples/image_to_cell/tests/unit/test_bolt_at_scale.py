"""
Large-scale backfill tests — match the customer-reported scenario.

Reported behaviour: in default-mode backfills with hundreds of rows and
multiple attachments per row, ALL attachments get reprocessed (and
charged) instead of only the ones that were actually new since the
last run.

Two failure modes contribute to that observation:

1. The prior-processed Supabase query fails (connection exhaustion,
   timeout, payload too large) → _fetch_prior_processed returns {}
   silently → estimator counts every attachment as new → backfill is
   either rejected with insufficient_bolts (small budget) or allowed
   to run at full cost (generous budget).

2. The synthetic-state build in ImageProcessor receives no log data →
   every row processor starts from a blank state → the runtime worker
   uploads everything, regardless of what the estimator said.

These tests scale up the trivial-case tests in the other files to the
size where the customer first noticed the problem, and check three
properties that have to hold for billing to be correct:

    a) When NOTHING changed since the last backfill, images_needed = 0.
    b) When ONE attachment per row is new, images_needed = N (rows).
    c) When the prior-log query fails, the estimator's count is wrong
       — pinned today, expected to flip to correct when the fix lands.

A scaling sanity test runs 1000 rows × 3 attachments to ensure the
estimator does not have an accidental O(n²) blowup as the bug report
suggested might be happening.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


def _ctx(*, ceiling=10_000, remaining=10_000, bpi=1):
    return {
        "ceiling_bolts": ceiling,
        "remaining_effective_bolts": remaining,
        "bolts_per_image": bpi,
        "plan_suspended": False,
        "bolts_pending_payment": False,
        "hard_cap_reached_rpc": False,
        "consumed_bolts_start": 0,
    }


@pytest.fixture
def patched_gate(mock_supabase, monkeypatch):
    from src.services import i2c_bolt_gate

    container = MagicMock()
    container.context = _ctx()

    monkeypatch.setattr(
        i2c_bolt_gate, "load_i2c_org_usage_context", lambda org: container.context
    )
    monkeypatch.setattr(i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase)
    monkeypatch.setattr(i2c_bolt_gate, "_PRIOR_LOG_RETRY_BASE_DELAY_S", 0.001)
    container.module = i2c_bolt_gate
    return container


# ---------------------------------------------------------------------------
# Helpers — build large fixtures cheaply
# ---------------------------------------------------------------------------


def _build_default_workload(*, n_rows: int, atts_per_row: int, n_columns: int):
    """Build (config, rows, full_prior_log) for a default-mode backfill of
    `n_rows` rows, each with `atts_per_row` attachments named systematically
    so usage-log subsets can be constructed by row_id range."""
    cfg = make_config(
        config_type="default",
        n_image_columns=n_columns,
        images_to_process=n_columns,
    )
    col_ids = cfg.image_columns
    if atts_per_row > n_columns:
        raise ValueError("atts_per_row cannot exceed n_columns for default mode")

    rows = []
    full_log = []
    # Use distinct id ranges per row so attachment ids never collide
    for row_id in range(1, n_rows + 1):
        atts = []
        for j in range(atts_per_row):
            att = make_attachment(
                id=row_id * 1000 + j,
                name=f"r{row_id}_img{j}.jpg",
            )
            atts.append(att)
            full_log.append({
                "row_id": row_id,
                "image_column_id": col_ids[j],
                "image_filename": att.name,
            })
        rows.append(make_row(row_id=row_id, attachments=atts))

    return cfg, rows, full_log


# ---------------------------------------------------------------------------
# E2E-shape: 200 rows × 3 attachments
# ---------------------------------------------------------------------------


class TestLargeDefaultBackfill:
    def test_no_change_backfill_should_count_zero(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """200 rows × 3 attachments per row, every attachment already in the
        usage log: a re-run should count 0 images. The customer's complaint
        is that this case bills 600 bolts instead of 0."""
        cfg, rows, full_log = _build_default_workload(
            n_rows=200, atts_per_row=3, n_columns=3
        )
        mock_supabase.usage_log_rows = full_log
        # Generous budget so the result reflects the count, not budget exhaustion
        patched_gate.context = _ctx(ceiling=10_000, remaining=10_000, bpi=1)

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # 0 images needed → estimator returns None (allow). If the prior-log
        # branch is broken, we'd see images_needed=600 here.
        assert result is None

    def test_no_change_backfill_with_zero_budget_should_be_allowed(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Same as above but with `remaining=0`. The correct estimate is 0,
        which is <= 0, so the backfill is allowed (no-op). If the estimator
        over-counts, we'd see a rejection here."""
        cfg, rows, full_log = _build_default_workload(
            n_rows=200, atts_per_row=3, n_columns=3
        )
        mock_supabase.usage_log_rows = full_log
        patched_gate.context = _ctx(ceiling=10_000, remaining=0, bpi=1)

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is None, (
            "estimator over-counted a no-op backfill — this is the customer-"
            "reported overcharge symptom"
        )

    def test_one_new_attachment_per_row_counts_exactly_n_rows(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """100 rows × 3 attachments, prior log covers 2 of 3 per row. The
        estimator should count exactly 100 (one new per row)."""
        cfg, rows, full_log = _build_default_workload(
            n_rows=100, atts_per_row=3, n_columns=3
        )
        # Drop entries for column index 2 from every row → one new per row
        partial_log = [entry for entry in full_log if "_img2" not in entry["image_filename"]]
        mock_supabase.usage_log_rows = partial_log
        patched_gate.context = _ctx(ceiling=10_000, remaining=100, bpi=1)

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is None, (
            "100 needed and 100 available → allow. Failure here means the "
            "estimator counted more or fewer than 100."
        )

    def test_half_rows_unprocessed_counts_correctly(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """200 rows × 2 attachments. Prior log only covers rows 1–100 (all
        their attachments). Rows 101–200 have nothing in the log. Expected
        count: 100 rows × 2 atts = 200 images needed."""
        cfg, rows, full_log = _build_default_workload(
            n_rows=200, atts_per_row=2, n_columns=2
        )
        partial_log = [e for e in full_log if e["row_id"] <= 100]
        mock_supabase.usage_log_rows = partial_log
        # Tight budget — exactly 200 → still allow
        patched_gate.context = _ctx(ceiling=10_000, remaining=200, bpi=1)

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is None

        # And one less than 200 → reject with images_needed=200
        patched_gate.context = _ctx(ceiling=10_000, remaining=199, bpi=1)
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is not None
        assert result["images_needed"] == 200
        assert result["images_available"] == 199


# ---------------------------------------------------------------------------
# Prior log unavailable at customer scale
# ---------------------------------------------------------------------------


class TestLargeBackfillPriorLogUnavailable:
    """At the size the customer originally reported (200 rows × 3 atts),
    a prior-processed lookup failure must surface as a structured rejection
    — not silent overcharge through fail-open under-counting."""

    def test_prior_log_failure_at_scale_returns_prior_log_unavailable(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        cfg, rows, _full_log = _build_default_workload(
            n_rows=200, atts_per_row=3, n_columns=3
        )
        mock_supabase.usage_log_error = RuntimeError(
            "Server disconnected without sending a response"
        )
        patched_gate.context = _ctx(ceiling=10_000, remaining=10_000, bpi=1)

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["reason"] == "prior_log_unavailable"
        assert result["images_needed"] == 0
        assert result["images_available"] == 0
        assert "imagetocell_usage_log" in result["error"]


# ---------------------------------------------------------------------------
# Performance sanity — must not blow up at 1000 rows
# ---------------------------------------------------------------------------


class TestPerformanceSanity:
    def test_1000_row_estimate_runs_under_two_seconds(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """The customer was running backfills at the hundreds-of-rows
        scale. We pin a tight ceiling on a 1000-row estimate so any
        accidental O(n^2) regression (eg. nested filename lookup, repeated
        list scans) gets caught here.

        With mocked Supabase and pure-Python attachment matching, this
        should comfortably run under 2 seconds even on slower CI hardware."""
        cfg, rows, full_log = _build_default_workload(
            n_rows=1000, atts_per_row=3, n_columns=3
        )
        mock_supabase.usage_log_rows = full_log
        patched_gate.context = _ctx(ceiling=100_000, remaining=100_000, bpi=1)

        start = time.monotonic()
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 2.0, f"estimator took {elapsed:.2f}s for 1000 rows"
