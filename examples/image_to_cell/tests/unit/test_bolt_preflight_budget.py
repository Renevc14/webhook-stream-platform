"""
Tests for i2c_bolt_gate.preflight_backfill_budget.

This is the cost estimator that runs before a backfill begins. It must
return:
    None — when the backfill may proceed (no ceiling, or images_needed
           <= images_available)
    dict — when the backfill would exceed the org's hard ceiling, with
           keys {reason, images_needed, images_available, bolts_per_image}
           so the caller can skip the backfill and surface the shortfall.

The estimator is the contract the customer is billed against. If it
under-counts (eg. fail-open returning {} when the prior-processed query
fails) the backfill is allowed to run and overcharges silently. If it
over-counts (eg. ignores already-placed images) legitimate backfills
are rejected with insufficient_bolts_for_backfill.

Tests cover:
    - Gate disabled / no ceiling → None (may proceed).
    - Context unavailable → blocking dict with reason=budget_unavailable
      (fail-closed; never authorize spend when Hub budget cannot be read).
    - Estimator None → reason=estimate_unavailable (fail-closed).
    - Per-mode counting accuracy (default, replace, name_map) including
      the prior-log subtraction path that prevents double-billing of
      already-processed images.
    - Bug repro: prior-log query failure under-counts and lets a
      backfill through that should be rejected (overcharge path).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


def _ctx(
    *,
    ceiling_bolts: int = 1000,
    remaining_effective_bolts: int = 1000,
    bolts_per_image: int = 1,
    plan_suspended: bool = False,
    bolts_pending_payment: bool = False,
    hard_cap_reached_rpc: bool = False,
    consumed_bolts_start: int = 0,
):
    """Build the dict load_i2c_org_usage_context returns."""
    return {
        "ceiling_bolts": ceiling_bolts,
        "remaining_effective_bolts": remaining_effective_bolts,
        "bolts_per_image": bolts_per_image,
        "plan_suspended": plan_suspended,
        "bolts_pending_payment": bolts_pending_payment,
        "hard_cap_reached_rpc": hard_cap_reached_rpc,
        "consumed_bolts_start": consumed_bolts_start,
    }


@pytest.fixture
def patched_gate(mock_supabase, monkeypatch):
    """Patch i2c_bolt_gate's external dependencies so tests control the
    org usage context AND the imagetocell_usage_log query response.

    Also collapses the retry backoff to near-zero so retry-path tests
    don't pay the 1+2+4s wall-clock cost in production code."""
    from src.services import i2c_bolt_gate

    container = MagicMock()
    container.context = _ctx()

    def fake_load_context(org_id: str):
        return container.context

    monkeypatch.setattr(i2c_bolt_gate, "load_i2c_org_usage_context", fake_load_context)
    monkeypatch.setattr(i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase)
    monkeypatch.setattr(i2c_bolt_gate, "_PRIOR_LOG_RETRY_BASE_DELAY_S", 0.001)
    container.module = i2c_bolt_gate
    return container


# ---------------------------------------------------------------------------
# Short-circuit cases — gate disabled / no ceiling / context unavailable
# ---------------------------------------------------------------------------


class TestPreflightShortCircuits:
    def test_returns_none_when_gate_disabled(
        self, patched_gate, mock_sheet_service, monkeypatch
    ):
        monkeypatch.setenv("I2C_BOLT_GATE_ENABLED", "false")
        cfg = make_config(config_type="default")
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, [], mock_sheet_service
        )
        assert result is None

    def test_blocks_when_context_unavailable(
        self, patched_gate, mock_sheet_service
    ):
        """Fail-closed: unknown Hub budget must not authorize a backfill."""
        patched_gate.context = None
        cfg = make_config(config_type="default")
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, [], mock_sheet_service
        )
        assert result is not None
        assert result["reason"] == "budget_unavailable"

    def test_returns_none_when_no_hard_ceiling(
        self, patched_gate, mock_sheet_service
    ):
        """Paid plans without hard_cap_percentage permit overage; backfills
        allowed regardless of cost."""
        patched_gate.context = _ctx(ceiling_bolts=None)
        cfg = make_config(config_type="default")
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, [], mock_sheet_service
        )
        assert result is None

    def test_returns_none_for_empty_rows(self, patched_gate, mock_sheet_service):
        cfg = make_config(config_type="default")
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, [], mock_sheet_service
        )
        assert result is None  # 0 needed <= available

    def test_blocks_when_estimator_returns_none(
        self, patched_gate, mock_sheet_service, monkeypatch
    ):
        """Fail-closed: estimator crash/unavailable must not authorize spend."""
        patched_gate.context = _ctx(ceiling_bolts=100, remaining_effective_bolts=50)
        monkeypatch.setattr(
            patched_gate.module,
            "compute_backfill_estimate",
            lambda *a, **k: None,
        )
        cfg = make_config(config_type="default")
        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id,
            cfg,
            [],
            mock_sheet_service,
        )
        assert result is not None
        assert result["reason"] == "estimate_unavailable"
        assert result["images_needed"] == 0



class TestDefaultModeCounting:
    def test_three_unprocessed_rows_count_three_images(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """3 rows, each with 1 attachment, no prior log → estimator says 3."""
        cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
        rows = [
            make_row(row_id=1, attachments=[make_attachment(id=1, name="a.jpg")]),
            make_row(row_id=2, attachments=[make_attachment(id=2, name="b.jpg")]),
            make_row(row_id=3, attachments=[make_attachment(id=3, name="c.jpg")]),
        ]
        mock_supabase.usage_log_rows = []  # nothing prior
        # Tight budget — exactly enough for the count we expect
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=3, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # 3 needed, 3 available → allow
        assert result is None

    def test_default_subtracts_already_placed_via_prior_log(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Two rows, each 3 attachments. Row 1 already has all 3 placed
        (via prior log), Row 2 has none. Default mode counts 0 for row 1
        (locked_columns=[0,1,2], images_to_process=3 → 0 slots left) and
        3 for row 2."""
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        row1_atts = [make_attachment(id=10 + i, name=f"r1-{i}.jpg") for i in range(3)]
        row2_atts = [make_attachment(id=20 + i, name=f"r2-{i}.jpg") for i in range(3)]
        rows = [
            make_row(row_id=1, attachments=row1_atts),
            make_row(row_id=2, attachments=row2_atts),
        ]
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_ids[i], "image_filename": f"r1-{i}.jpg"}
            for i in range(3)
        ]
        # Budget too tight to allow row 1 again — proves we did NOT count it
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=3, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # 3 needed (row 2 only), 3 available → allow
        assert result is None

    def test_default_rejects_when_over_budget(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
        rows = [
            make_row(row_id=i, attachments=[make_attachment(id=i, name=f"x{i}.jpg")])
            for i in range(1, 6)  # 5 rows
        ]
        mock_supabase.usage_log_rows = []
        # Only 2 bolts available → 5 needed → reject
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=2, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is not None
        assert result["reason"] == "backfill_budget_insufficient"
        assert result["images_needed"] == 5
        assert result["images_available"] == 2
        assert result["bolts_per_image"] == 1


# ---------------------------------------------------------------------------
# Replace mode counting (the commercial path that's already correct here)
# ---------------------------------------------------------------------------


class TestReplaceModeCounting:
    def test_replace_subtracts_already_placed_filenames(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Replace mode estimator: row has 3 attachments, all 3 names match
        prior-log entries for the target columns. images_needed should be 0
        for that row (no work to do — sheet matches log).

        Note: the implementation in i2c_bolt_gate handles replace correctly
        even though the runtime worker (ReplaceMode.filter_attachments)
        does not — see test_bolt_synthetic_state_replace_mode.py xfails.
        Estimate vs reality currently diverge for replace; this test pins
        the correct estimate side."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_ids[i], "image_filename": f"img{i}.jpg"}
            for i in range(3)
        ]
        # Zero budget — only allowed if estimator says 0 needed
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=0, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # 0 needed, 0 available → allow (no-op backfill)
        assert result is None

    def test_replace_counts_only_filenames_not_in_prior_log(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Row has 3 attachments, only 1 has a name not in the prior log.
        images_needed for that row == 1 (the new one)."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        atts = [
            make_attachment(id=10, name="old1.jpg"),
            make_attachment(id=11, name="old2.jpg"),
            make_attachment(id=12, name="brandnew.jpg"),
        ]
        rows = [make_row(row_id=1, attachments=atts)]
        # Only old1, old2 are in the log → brandnew should count as needing work
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_ids[0], "image_filename": "old1.jpg"},
            {"row_id": 1, "image_column_id": col_ids[1], "image_filename": "old2.jpg"},
        ]
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=1, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # Estimator should say 1 needed → exactly equals available → allow
        assert result is None


# ---------------------------------------------------------------------------
# Name_map mode counting
# ---------------------------------------------------------------------------


class TestNameMapCounting:
    def test_name_map_subtracts_processed_via_synthetic_state(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Same shape as default — synthetic state populates last_ids and
        last_ts, NameMapMode.filter_attachments returns 0 → 0 needed."""
        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            images_to_process=2,
            column_name_prefix="part",
        )
        col_ids = cfg.image_columns

        a1 = make_attachment(id=10, name="part-1.jpg")
        a2 = make_attachment(id=11, name="part-2.jpg")
        rows = [make_row(row_id=1, attachments=[a1, a2])]
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_ids[0], "image_filename": "part-1.jpg"},
            {"row_id": 1, "image_column_id": col_ids[1], "image_filename": "part-2.jpg"},
        ]
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=0, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        assert result is None

    def test_name_map_excludes_non_matching_attachments_from_count(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Row has 5 attachments, only 2 match column names. estimator
        should count 2 — irrespective of the other 3."""
        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            images_to_process=2,
            column_name_prefix="part",
        )
        atts = [
            make_attachment(id=1, name="part-1.jpg"),
            make_attachment(id=2, name="random.jpg"),
            make_attachment(id=3, name="part-2.jpg"),
            make_attachment(id=4, name="screenshot.jpg"),
            make_attachment(id=5, name="receipt.jpg"),
        ]
        rows = [make_row(row_id=1, attachments=atts)]
        mock_supabase.usage_log_rows = []  # nothing prior
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=2, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )
        # 2 needed (only the part-* atts), 2 available → allow
        assert result is None


# ---------------------------------------------------------------------------
# Bug repro — fail-open under-counting
# ---------------------------------------------------------------------------


class TestPreflightPriorLogUnavailable:
    """When the prior-processed lookup fails after all retries, the estimator
    must surface the failure as a structured rejection — never silently treat
    every row as unprocessed (which would re-bill already-processed images)."""

    def test_query_failure_returns_prior_log_unavailable_after_retries(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        mock_supabase.usage_log_error = RuntimeError(
            "Server disconnected without sending a response"
        )
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=10, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["reason"] == "prior_log_unavailable"
        # No false positive count — we did not pretend zero is the right answer
        assert result["images_needed"] == 0
        assert result["images_available"] == 0
        assert "imagetocell_usage_log" in result["error"]

    def test_query_retried_three_times_before_failing(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """The retry budget is exactly 3 attempts. We count how many times
        the mock raises before preflight gives up."""
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        rows = [
            make_row(row_id=1, attachments=[make_attachment(id=1, name="img.jpg")])
        ]
        mock_supabase.usage_log_error = RuntimeError("transient")
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=10, bolts_per_image=1
        )

        # Track how many times the chain.execute() gets invoked
        original_execute = mock_supabase.table("imagetocell_usage_log").execute
        call_count = {"n": 0}

        def counted_execute():
            call_count["n"] += 1
            return original_execute()

        mock_supabase.table("imagetocell_usage_log").execute = counted_execute

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert call_count["n"] == 3
        assert result["reason"] == "prior_log_unavailable"

    def test_transient_failure_then_success_recovers(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Two failures followed by a successful response should recover —
        the estimator should produce a valid count instead of giving up."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_ids = cfg.image_columns

        atts = [
            make_attachment(id=1, name="a.jpg"),
            make_attachment(id=2, name="b.jpg"),
        ]
        rows = [make_row(row_id=1, attachments=atts)]

        # The mock_supabase chain raises usage_log_error on every execute.
        # Configure a counter that makes the first 2 calls raise, third return.
        attempt_count = {"n": 0}
        original_execute_chain = mock_supabase.table("imagetocell_usage_log")

        def flaky_execute():
            attempt_count["n"] += 1
            if attempt_count["n"] <= 2:
                raise RuntimeError("transient")
            response = MagicMock()
            response.data = [
                {"row_id": 1, "image_column_id": col_ids[0], "image_filename": "a.jpg"},
                {"row_id": 1, "image_column_id": col_ids[1], "image_filename": "b.jpg"},
            ]
            return response

        original_execute_chain.execute = flaky_execute
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=10, bolts_per_image=1
        )

        result = patched_gate.module.preflight_backfill_budget(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        # Recovered on attempt 3, prior log subtracted both → 0 needed → allow
        assert attempt_count["n"] == 3
        assert result is None
