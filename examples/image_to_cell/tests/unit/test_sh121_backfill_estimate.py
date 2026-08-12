"""SH-121 — pre-backfill bolt usage estimate.

Covers both halves of the SH-121 refactor:

  1. ``i2c_bolt_gate.compute_backfill_estimate`` — the always-on estimator that
     returns the full breakdown (images, bolts, overage, blocked) regardless of
     whether the org has a hard ceiling. Both the existing enforcement path and
     the new UI preview endpoint share this single source of truth so they can
     never disagree.

  2. The wrapper ``preflight_backfill_budget`` keeps its legacy contract — that
     coverage lives in ``test_bolt_preflight_budget.py`` and must keep passing
     after this refactor.

  3. ``POST /configs/backfill/estimate`` — the read-only preview endpoint, with
     coverage for the no-configs short circuit, the aggregation of org-level
     totals, and the per-config ``estimate_unavailable`` failure mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.conftest import make_attachment, make_cell, make_config, make_row


def _ctx(
    *,
    ceiling_bolts=1000,
    remaining_effective_bolts=1000,
    bolts_per_image=15,
    consumed_bolts_start=0,
):
    return {
        "ceiling_bolts": ceiling_bolts,
        "remaining_effective_bolts": remaining_effective_bolts,
        "bolts_per_image": bolts_per_image,
        "plan_suspended": False,
        "bolts_pending_payment": False,
        "hard_cap_reached_rpc": False,
        "consumed_bolts_start": consumed_bolts_start,
    }


@pytest.fixture
def patched_gate(mock_supabase, monkeypatch):
    """Same shape as the fixture in test_bolt_preflight_budget — keeps the new
    tests using the same control surface."""
    from src.services import i2c_bolt_gate

    container = MagicMock()
    container.context = _ctx()

    def fake_load_context(org_id):
        return container.context

    monkeypatch.setattr(i2c_bolt_gate, "load_i2c_org_usage_context", fake_load_context)
    monkeypatch.setattr(i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase)
    monkeypatch.setattr(i2c_bolt_gate, "_PRIOR_LOG_RETRY_BASE_DELAY_S", 0.001)
    container.module = i2c_bolt_gate
    return container


# ---------------------------------------------------------------------------
# compute_backfill_estimate — short-circuit fail-safes
# ---------------------------------------------------------------------------


class TestEstimateUnavailable:
    """The estimator must return None — not zeros — when it cannot produce a
    trustworthy number. The endpoint surfaces this as ``estimate_unavailable``
    so the UI never shows a confident-looking but fabricated estimate."""

    def test_gate_disabled_returns_none(
        self, patched_gate, mock_sheet_service, monkeypatch
    ):
        monkeypatch.setenv("I2C_BOLT_GATE_ENABLED", "false")
        cfg = make_config(config_type="default")
        assert (
            patched_gate.module.compute_backfill_estimate(
                cfg.organization_id, cfg, [], mock_sheet_service
            )
            is None
        )

    def test_context_unavailable_returns_none(
        self, patched_gate, mock_sheet_service
    ):
        patched_gate.context = None
        cfg = make_config(config_type="default")
        assert (
            patched_gate.module.compute_backfill_estimate(
                cfg.organization_id, cfg, [], mock_sheet_service
            )
            is None
        )


# ---------------------------------------------------------------------------
# compute_backfill_estimate — paid plan, no ceiling
# ---------------------------------------------------------------------------


class TestPaidPlanNoCeiling:
    """Paid plans without a hard cap permit overage and bill accordingly. The
    estimator must report the full breakdown so the UI can show "this will run
    you N overage bolts — proceed?", which is the whole point of SH-121."""

    def test_overage_reported_but_not_blocked(
        self, patched_gate, mock_sheet_service
    ):
        # ceiling_bolts=None → paid plan with no hard cap
        patched_gate.context = _ctx(
            ceiling_bolts=None, remaining_effective_bolts=15, bolts_per_image=15
        )

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["has_ceiling"] is False
        assert result["blocked"] is False
        assert result["block_reason"] is None
        assert result["images_needed"] == 3
        assert result["bolts_per_image"] == 15
        assert result["bolts_needed"] == 45
        # The org has 15 bolts left in its allocation; the other 30 are overage.
        assert result["bolts_available_in_cap"] == 15
        assert result["bolts_overage_estimated"] == 30

    def test_under_allocation_no_overage(
        self, patched_gate, mock_sheet_service
    ):
        patched_gate.context = _ctx(
            ceiling_bolts=None, remaining_effective_bolts=1000, bolts_per_image=15
        )

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result["bolts_needed"] == 45
        assert result["bolts_overage_estimated"] == 0
        assert result["blocked"] is False


# ---------------------------------------------------------------------------
# compute_backfill_estimate — plans with a hard ceiling
# ---------------------------------------------------------------------------


class TestCeilingPlanFitsBudget:
    def test_within_budget_returns_breakdown_unblocked(
        self, patched_gate, mock_sheet_service
    ):
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=1000, bolts_per_image=15
        )

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result["has_ceiling"] is True
        assert result["blocked"] is False
        assert result["images_needed"] == 3
        assert result["bolts_needed"] == 45
        assert result["bolts_overage_estimated"] == 0


class TestCeilingPlanExceedsBudget:
    def test_over_budget_marks_blocked(
        self, patched_gate, mock_sheet_service
    ):
        # remaining 30 → 2 affordable images at 15 bolts each; we ask for 3.
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=30, bolts_per_image=15
        )

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result["has_ceiling"] is True
        assert result["blocked"] is True
        assert result["block_reason"] == "backfill_budget_insufficient"
        assert result["images_needed"] == 3
        assert result["images_available"] == 2  # remaining_eff // bolts_per_image
        assert result["bolts_needed"] == 45
        assert result["bolts_overage_estimated"] == 15  # 45 needed - 30 available


# ---------------------------------------------------------------------------
# compute_backfill_estimate — prior history subtracts processed work
# ---------------------------------------------------------------------------


class TestPriorHistory:
    """If the customer has already processed some attachments in earlier runs,
    those must NOT count toward this backfill's estimate — same logic as the
    legacy preflight enforces, exposed through the new breakdown."""

    def test_replace_mode_subtracts_already_processed(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=1000, bolts_per_image=15
        )

        cfg = make_config(
            config_type="replace", n_image_columns=3, images_to_process=3
        )
        atts = [
            make_attachment(id=10, name="a.jpg"),
            make_attachment(id=11, name="b.jpg"),
            make_attachment(id=12, name="c.jpg"),
        ]
        rows = [make_row(row_id=1, attachments=atts)]
        # b.jpg and c.jpg already processed previously.
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": cfg.image_columns[0], "image_filename": "b.jpg"},
            {"row_id": 1, "image_column_id": cfg.image_columns[1], "image_filename": "c.jpg"},
        ]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result["images_needed"] == 1
        assert result["bolts_needed"] == 15


# ---------------------------------------------------------------------------
# compute_backfill_estimate — SH-108 empty-cell prior-log prune (parity
# with ImageProcessor._build_synthetic_states_for_backfill)
# ---------------------------------------------------------------------------


class TestSh108EmptyCellPriorLogEstimate:
    """Cleared target cells must not be treated as already-done via
    usage_log. Runtime synthetic-state already prunes those entries;
    the estimator must use the same ``filter_prior_for_filled_cells``
    helper so SH-183 Run Backfill stays enabled after the user clears
    cells."""

    def test_cleared_cells_with_prior_log_count_as_needed(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Prior success + empty cell → images_needed > 0 (reprocess)."""
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=1000, bolts_per_image=15
        )
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att = make_attachment(id=500, name="dog.5.jpg")
        rows = [
            make_row(
                row_id=9001,
                attachments=[att],
                cells=[
                    make_cell(column_id=col_a),  # cleared
                    make_cell(column_id=col_b),
                ],
            )
        ]
        mock_supabase.usage_log_rows = [
            {
                "row_id": 9001,
                "image_column_id": col_a,
                "image_filename": "dog.5.jpg",
            },
        ]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["images_needed"] == 1
        assert result["bolts_needed"] == 15
        assert result["blocked"] is False

    def test_filled_cells_with_prior_log_still_skip(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Cell still holds the prior image → images_needed = 0."""
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=1000, bolts_per_image=15
        )
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att = make_attachment(id=500, name="dog.5.jpg")
        rows = [
            make_row(
                row_id=9002,
                attachments=[att],
                cells=[
                    make_cell(column_id=col_a, image_alt="dog.5.jpg"),
                    make_cell(column_id=col_b),
                ],
            )
        ]
        mock_supabase.usage_log_rows = [
            {
                "row_id": 9002,
                "image_column_id": col_a,
                "image_filename": "dog.5.jpg",
            },
        ]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["images_needed"] == 0
        assert result["bolts_needed"] == 0

    def test_replace_mode_cleared_cell_does_not_subtract_prior(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        """Replace mode also applies SH-108 before subtracting filenames."""
        patched_gate.context = _ctx(
            ceiling_bolts=1000, remaining_effective_bolts=1000, bolts_per_image=15
        )
        cfg = make_config(
            config_type="replace", n_image_columns=2, images_to_process=2
        )
        col_a, col_b = cfg.image_columns
        atts = [
            make_attachment(id=10, name="a.jpg"),
            make_attachment(id=11, name="b.jpg"),
        ]
        rows = [
            make_row(
                row_id=1,
                attachments=atts,
                cells=[
                    make_cell(column_id=col_a),  # cleared
                    make_cell(column_id=col_b),
                ],
            )
        ]
        # Prior log claims both were processed, but cells are empty now.
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_a, "image_filename": "a.jpg"},
            {"row_id": 1, "image_column_id": col_b, "image_filename": "b.jpg"},
        ]

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        # Without SH-108 this would be 0; with prune both count again.
        assert result["images_needed"] == 2


# ---------------------------------------------------------------------------
# compute_backfill_estimate — prior log query failure surfaces as blocked
# ---------------------------------------------------------------------------


class TestPriorLogUnavailable:
    def test_query_failure_surfaces_explicit_block(
        self, patched_gate, mock_supabase, mock_sheet_service
    ):
        patched_gate.context = _ctx(
            ceiling_bolts=10, remaining_effective_bolts=10, bolts_per_image=1
        )
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=10 + i, name=f"img{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]

        mock_supabase.usage_log_error = RuntimeError(
            "Server disconnected without sending a response"
        )

        result = patched_gate.module.compute_backfill_estimate(
            cfg.organization_id, cfg, rows, mock_sheet_service
        )

        assert result is not None
        assert result["blocked"] is True
        assert result["block_reason"] == "prior_log_unavailable"
        # Conservative zeros — we did not pretend any number was reliable.
        assert result["images_needed"] == 0
        assert result["images_available"] == 0
        assert result["bolts_needed"] == 0
        assert result["bolts_available_in_cap"] == 0
        assert "imagetocell_usage_log" in result["error"]


# ---------------------------------------------------------------------------
# POST /configs/backfill/estimate — endpoint behaviour
# ---------------------------------------------------------------------------


class TestEstimateEndpoint:
    """The endpoint orchestrates: load configs → fetch rows → estimate each
    config → aggregate org-level totals → return. These tests use direct
    function invocation (not HTTPX) because the value is in the response shape
    and the aggregation logic, not the FastAPI plumbing."""

    def _make_config_obj(
        self, *, id_="cfg-1", name="My config", config_type="default", org_id="org-1"
    ):
        c = MagicMock()
        c.id = id_
        c.configuration_name = name
        c.organization_id = org_id
        c.config_type = MagicMock(value=config_type)
        return c

    @pytest.mark.asyncio
    async def test_no_configs_returns_empty_response(self):
        from src.api.routes.configs import estimate_backfill, BackfillEstimateRequest

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo:
            instance = MockRepo.return_value
            instance.get_active_webhook_configs.return_value = []

            response = await estimate_backfill(
                request=BackfillEstimateRequest(worksheet_id=12345, max_pages=10),
                org_id="org-1",
                db=MagicMock(),
            )

        assert response.worksheet_id == 12345
        assert response.existing_configs == []
        assert response.totals.bolts_needed == 0
        assert response.totals.any_blocked is False

    @pytest.mark.asyncio
    async def test_aggregates_org_level_totals_correctly(self):
        from src.api.routes.configs import estimate_backfill, BackfillEstimateRequest

        cfg1 = self._make_config_obj(id_="cfg-1", name="C1")
        cfg2 = self._make_config_obj(id_="cfg-2", name="C2")

        # cfg1: 30 bolts needed; cfg2: 45 bolts needed. Org cap is 25 → total
        # overage = 50 (not naive 30+50 = sum of per-config overage).
        def fake_estimate(org_id, config, rows, sheet_service):
            if config.id == "cfg-1":
                return {
                    "images_needed": 2,
                    "images_available": 1,
                    "bolts_per_image": 15,
                    "bolts_needed": 30,
                    "bolts_available_in_cap": 25,
                    "bolts_overage_estimated": 5,
                    "has_ceiling": True,
                    "blocked": True,
                    "block_reason": "backfill_budget_insufficient",
                }
            return {
                "images_needed": 3,
                "images_available": 1,
                "bolts_per_image": 15,
                "bolts_needed": 45,
                "bolts_available_in_cap": 25,
                "bolts_overage_estimated": 20,
                "has_ceiling": True,
                "blocked": True,
                "block_reason": "backfill_budget_insufficient",
            }

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch("src.services.i2c_bolt_gate.compute_backfill_estimate", side_effect=fake_estimate):
            MockRepo.return_value.get_active_webhook_configs.return_value = [cfg1, cfg2]
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(
                return_value=[{"id": i} for i in range(5)]
            )

            response = await estimate_backfill(
                request=BackfillEstimateRequest(worksheet_id=999, max_pages=10),
                org_id="org-1",
                db=MagicMock(),
            )

        assert response.totals.bolts_needed == 75  # 30 + 45
        assert response.totals.bolts_available_in_cap == 25  # org-level, not 50
        assert response.totals.bolts_overage_estimated == 50  # max(0, 75 - 25)
        assert response.totals.any_blocked is True
        assert {c.config_id for c in response.existing_configs} == {"cfg-1", "cfg-2"}

    @pytest.mark.asyncio
    async def test_unavailable_estimate_surfaces_per_config_without_breaking_response(
        self,
    ):
        from src.api.routes.configs import estimate_backfill, BackfillEstimateRequest

        cfg1 = self._make_config_obj(id_="cfg-1", name="C1")
        cfg2 = self._make_config_obj(id_="cfg-2", name="C2")

        # cfg-1 returns a valid estimate; cfg-2 returns None (gate disabled,
        # context unavailable, or estimator crash).
        def fake_estimate(org_id, config, rows, sheet_service):
            if config.id == "cfg-1":
                return {
                    "images_needed": 2,
                    "images_available": 10,
                    "bolts_per_image": 15,
                    "bolts_needed": 30,
                    "bolts_available_in_cap": 150,
                    "bolts_overage_estimated": 0,
                    "has_ceiling": True,
                    "blocked": False,
                    "block_reason": None,
                }
            return None

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch("src.services.i2c_bolt_gate.compute_backfill_estimate", side_effect=fake_estimate):
            MockRepo.return_value.get_active_webhook_configs.return_value = [cfg1, cfg2]
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(
                return_value=[{"id": 1}]
            )

            response = await estimate_backfill(
                request=BackfillEstimateRequest(worksheet_id=999, max_pages=10),
                org_id="org-1",
                db=MagicMock(),
            )

        cfg1_entry = next(c for c in response.existing_configs if c.config_id == "cfg-1")
        cfg2_entry = next(c for c in response.existing_configs if c.config_id == "cfg-2")
        assert cfg1_entry.error is None
        assert cfg1_entry.bolts_needed == 30
        # cfg-2 carries the explicit error marker rather than misleading zeros.
        assert cfg2_entry.error == "estimate_unavailable"
        assert cfg2_entry.bolts_needed == 0
        # Totals only include the usable estimates' cap value.
        assert response.totals.bolts_available_in_cap == 150
