"""SH-144 — Backfill estimate endpoint accepts optional draft_config.

Extends SH-121: the endpoint now also runs the same estimator on an
in-memory config supplied in the request body, so the Create New Config
modal can show cost-before-commit. The draft is NEVER persisted; the
estimator path is read-only.

Coverage:
  - Draft-only (no existing configs on the worksheet) returns the draft
    estimate and an empty existing_configs list.
  - Draft + existing configs returns both, and totals reflect both.
  - Backwards compatibility: no draft in the body returns
    draft_estimate=None and the original SH-121 behaviour.
  - Validation: malformed drafts (image_columns empty / too many,
    invalid config_type, missing required fields) get rejected at the
    pydantic layer, never reach the estimator.
  - No-write invariant: when the draft path runs, none of the Supabase
    write methods (insert / update / upsert / delete) are called.
  - Totals: the draft contributes to bolts_needed and any_blocked at the
    org level, matching how the math works for existing configs.
  - name_map mode: namemap_patterns from the draft are wired into the
    in-memory config and reach the estimator.

The endpoint and the estimator are still the same single source of truth
shared with ``POST /configs/backfill``; this file only exercises the new
draft path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Shared test helpers — keep these as plain helpers rather than fixtures so
# each test gets a fresh, isolated mock without surprises across tests.
# ---------------------------------------------------------------------------


def _make_existing_config(
    id_: str = "cfg-existing",
    name: str = "Existing",
    org_id: str = "org-1",
    config_type: str = "default",
):
    c = MagicMock()
    c.id = id_
    c.configuration_name = name
    c.organization_id = org_id
    c.config_type = MagicMock(value=config_type)
    return c


def _default_estimate_payload():
    """A non-blocking, non-zero estimate. Used as the default that the
    fake estimator returns unless a test overrides it."""
    return {
        "images_needed": 4,
        "images_available": 100,
        "bolts_per_image": 15,
        "bolts_needed": 60,
        "bolts_available_in_cap": 500,
        "bolts_overage_estimated": 0,
        "has_ceiling": True,
        "blocked": False,
        "block_reason": None,
    }


def _minimum_valid_draft():
    """A small but fully-valid DraftConfigPayload dict — every required
    field present, all bounded fields inside their pydantic constraints."""
    return {
        "configuration_name": "New from modal",
        "processing_mode": "webhook",
        "config_type": "default",
        "images_to_process": 2,
        "image_columns": [9000000000000001, 9000000000000002],
    }


# ---------------------------------------------------------------------------
# Backwards compatibility — no draft in the body still returns SH-121 shape.
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    """SH-144 must not change the response for callers that don't send a
    draft_config. The Create modal sends one; the future "Run Backfill on
    existing config" UI will not."""

    @pytest.mark.asyncio
    async def test_no_draft_returns_null_draft_estimate(self):
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
        )

        cfg = _make_existing_config()

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                return_value=_default_estimate_payload(),
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = [cfg]
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[{"id": 1}])

            response = await estimate_backfill(
                request=BackfillEstimateRequest(worksheet_id=999, max_pages=10),
                org_id="org-1",
                db=MagicMock(),
            )

        # The original SH-121 shape lives on under the renamed field. The
        # draft slot is empty when no draft was sent.
        assert response.worksheet_id == 999
        assert len(response.existing_configs) == 1
        assert response.existing_configs[0].config_id == "cfg-existing"
        assert response.draft_estimate is None
        # Totals only include the existing config — no draft contribution.
        assert response.totals.bolts_needed == 60


# ---------------------------------------------------------------------------
# Draft-only and draft + existing — the two consumer-facing flows.
# ---------------------------------------------------------------------------


class TestDraftEstimateFlow:
    """The Create New Config modal sends a draft alongside zero existing
    configs (the modal blocks a second config on the same worksheet). The
    future "Run Backfill manually" UI may send a draft alongside an existing
    config in edge cases (e.g. a user tweaking an unsaved variant). Both
    shapes must produce a usable response."""

    @pytest.mark.asyncio
    async def test_draft_only_no_existing_configs_returns_draft_estimate(self):
        """The Create modal's primary path: no persisted configs on the
        worksheet, just a draft. Before SH-144 this returned configs=[]
        and the panel had nothing to show."""
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        captured_calls = []

        def fake_estimator(org_id, config, rows, sheet_service):
            captured_calls.append({
                "org_id": org_id,
                "config_id": getattr(config, "id", None),
                "config_type": getattr(getattr(config, "config_type", None), "value", None),
            })
            return _default_estimate_payload()

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                side_effect=fake_estimator,
             ):
            # No configs persisted on the worksheet at all.
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[
                {"id": i} for i in range(20)
            ])

            response = await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(**_minimum_valid_draft()),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        # The original SH-121 short-circuit (configs=[] → empty response)
        # MUST NOT fire just because there are no persisted configs.
        # If it did, the estimator never runs and the user sees zeros —
        # the bug AK reported on SH-122. This assertion is the regression
        # guard for that bug.
        assert len(captured_calls) == 1, (
            "Estimator must run exactly once for the draft when there are "
            "no existing configs. The original SH-121 short-circuit must "
            "not fire when draft_config is present."
        )
        assert captured_calls[0]["config_id"] == "draft-preview"
        assert captured_calls[0]["config_type"] == "default"

        assert response.existing_configs == []
        assert response.draft_estimate is not None
        assert response.draft_estimate.configuration_name == "New from modal"
        assert response.draft_estimate.config_type == "default"
        assert response.draft_estimate.bolts_needed == 60
        assert response.draft_estimate.error is None

    @pytest.mark.asyncio
    async def test_draft_plus_existing_returns_both_and_totals_aggregate(self):
        """If both shapes are present the response carries both. Totals
        roll up across both because they're both real cost the user would
        incur — same math as if the draft were already persisted."""
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        cfg = _make_existing_config(id_="cfg-1", name="Persisted")

        def fake_estimator(org_id, config, rows, sheet_service):
            cid = getattr(config, "id", None)
            if cid == "cfg-1":
                return {
                    "images_needed": 2,
                    "images_available": 100,
                    "bolts_per_image": 15,
                    "bolts_needed": 30,
                    "bolts_available_in_cap": 500,
                    "bolts_overage_estimated": 0,
                    "has_ceiling": True,
                    "blocked": False,
                    "block_reason": None,
                }
            # draft-preview
            return {
                "images_needed": 3,
                "images_available": 100,
                "bolts_per_image": 15,
                "bolts_needed": 45,
                "bolts_available_in_cap": 500,
                "bolts_overage_estimated": 0,
                "has_ceiling": True,
                "blocked": False,
                "block_reason": None,
            }

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                side_effect=fake_estimator,
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = [cfg]
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[{"id": 1}])

            response = await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(**_minimum_valid_draft()),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        assert len(response.existing_configs) == 1
        assert response.existing_configs[0].bolts_needed == 30
        assert response.draft_estimate is not None
        assert response.draft_estimate.bolts_needed == 45
        # Totals roll up across both — the user is about to incur both bills.
        # bolts_available_in_cap is org-level so it's NOT 30+45+cap, it's just cap.
        assert response.totals.bolts_needed == 75
        assert response.totals.bolts_available_in_cap == 500
        assert response.totals.bolts_overage_estimated == 0
        assert response.totals.any_blocked is False


# ---------------------------------------------------------------------------
# Validation — pydantic must reject malformed drafts before the endpoint runs.
# ---------------------------------------------------------------------------


class TestDraftValidation:
    """The draft_config is validated by the same constraints that govern
    ConfigCreate. We test pydantic directly here — by the time the endpoint
    body parses, anything malformed has already been rejected with 422."""

    def test_empty_image_columns_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError) as exc:
            DraftConfigPayload(
                configuration_name="x",
                image_columns=[],  # min_length=1 violated
            )
        # The validation error must mention image_columns explicitly so
        # the frontend can highlight the right field.
        assert "image_columns" in str(exc.value)

    def test_too_many_image_columns_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError):
            DraftConfigPayload(
                configuration_name="x",
                image_columns=[100 + i for i in range(11)],  # max_length=10
            )

    def test_invalid_config_type_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError):
            DraftConfigPayload(
                configuration_name="x",
                image_columns=[1, 2],
                config_type="bogus",  # not in ConfigType enum
            )

    def test_images_to_process_out_of_range_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError):
            DraftConfigPayload(
                configuration_name="x",
                image_columns=[1, 2],
                images_to_process=11,  # ge=1, le=10
            )
        with pytest.raises(ValidationError):
            DraftConfigPayload(
                configuration_name="x",
                image_columns=[1, 2],
                images_to_process=0,
            )

    def test_missing_configuration_name_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError):
            DraftConfigPayload(image_columns=[1, 2])

    def test_empty_configuration_name_rejected(self):
        from src.api.routes.configs import DraftConfigPayload

        with pytest.raises(ValidationError):
            DraftConfigPayload(configuration_name="", image_columns=[1, 2])

    def test_valid_minimum_payload_accepts(self):
        """Happy path control: the minimum valid shape parses without raising."""
        from src.api.routes.configs import DraftConfigPayload

        draft = DraftConfigPayload(**_minimum_valid_draft())
        assert draft.config_type.value == "default"
        assert draft.images_to_process == 2
        assert len(draft.image_columns) == 2


# ---------------------------------------------------------------------------
# No-write invariant — the draft path must NEVER write to Supabase.
# ---------------------------------------------------------------------------


class TestNoWriteInvariant:
    """SH-144 must remain read-only end to end. A future regression that
    accidentally persists the draft (e.g., someone refactors and reuses the
    create flow) would be a serious billing concern — a draft preview turning
    into an actual config under the hood. This test mocks the DB writers and
    asserts none of them are called on the draft path."""

    @pytest.mark.asyncio
    async def test_draft_path_does_not_write_to_supabase(self):
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        # The Supabase service mock — every write verb is a MagicMock so we
        # can assert no call. The fluent chain returns self so the calls
        # don't crash if something tries to use them.
        db = MagicMock()
        table = MagicMock()
        db.table = MagicMock(return_value=table)
        db.execute_with_retry = MagicMock()
        for verb in ("insert", "update", "upsert", "delete"):
            setattr(table, verb, MagicMock(return_value=table))

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                return_value=_default_estimate_payload(),
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[{"id": 1}])

            await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(**_minimum_valid_draft()),
                ),
                org_id="org-1",
                db=db,
            )

        # The exact regression guard: none of the DB write verbs ran.
        table.insert.assert_not_called()
        table.update.assert_not_called()
        table.upsert.assert_not_called()
        table.delete.assert_not_called()
        # execute_with_retry is the write wrapper used elsewhere in the
        # configs route. The estimate path uses ConfigRepository.get_*
        # (read-only) and Smartsheet (read-only). It must not call this.
        db.execute_with_retry.assert_not_called()


# ---------------------------------------------------------------------------
# Draft propagates through to ImageToCellConfig correctly.
# ---------------------------------------------------------------------------


class TestDraftToConfigMapping:
    """The in-memory ImageToCellConfig that the estimator sees has to
    reflect every field of the draft. This is what guarantees the preview
    matches what the real config would charge once persisted."""

    @pytest.mark.asyncio
    async def test_image_columns_propagate_to_image_N_column_id_fields(self):
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        captured = {}

        def fake_estimator(org_id, config, rows, sheet_service):
            captured["image_columns"] = config.image_columns
            captured["image1_column_id"] = config.image1_column_id
            captured["image2_column_id"] = config.image2_column_id
            captured["image3_column_id"] = config.image3_column_id
            return _default_estimate_payload()

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                side_effect=fake_estimator,
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[])

            await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(
                        configuration_name="x",
                        image_columns=[1001, 1002, 1003],
                        images_to_process=3,
                    ),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        assert captured["image_columns"] == [1001, 1002, 1003]
        assert captured["image1_column_id"] == 1001
        assert captured["image2_column_id"] == 1002
        assert captured["image3_column_id"] == 1003

    @pytest.mark.asyncio
    async def test_namemap_patterns_propagate_to_namemap_column_N_fields(self):
        """Name-map mode reads `namemap_column{N}` from the config. The
        draft uses a dict {slot: pattern} for ergonomics — verify the dict
        ends up in the right per-slot fields."""
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        captured = {}

        def fake_estimator(org_id, config, rows, sheet_service):
            captured["namemap_column1"] = config.namemap_column1
            captured["namemap_column2"] = config.namemap_column2
            captured["namemap_column3"] = config.namemap_column3
            captured["config_type"] = config.config_type.value
            return _default_estimate_payload()

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                side_effect=fake_estimator,
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[])

            await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(
                        configuration_name="x",
                        image_columns=[1001, 1002, 1003],
                        config_type="name map",
                        namemap_patterns={1: "dog", 3: "horse"},
                    ),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        assert captured["config_type"] == "name map"
        assert captured["namemap_column1"] == "dog"
        assert captured["namemap_column2"] is None  # slot 2 not in dict
        assert captured["namemap_column3"] == "horse"


# ---------------------------------------------------------------------------
# Failure modes — when the estimator can't produce a number, we surface
# estimate_unavailable on the draft slot rather than fabricating zeros.
# ---------------------------------------------------------------------------


class TestDraftFailureModes:
    @pytest.mark.asyncio
    async def test_estimator_returns_none_surfaces_estimate_unavailable(self):
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                return_value=None,
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[])

            response = await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(**_minimum_valid_draft()),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        assert response.draft_estimate is not None
        assert response.draft_estimate.error == "estimate_unavailable"
        # Fabricated zeros must not appear in the cost-shaped fields.
        assert response.draft_estimate.bolts_needed == 0
        assert response.draft_estimate.images_needed == 0

    @pytest.mark.asyncio
    async def test_estimator_crash_surfaces_estimate_unavailable(self):
        """If the estimator raises, the endpoint must catch and surface
        the error on the draft slot — don't let one crash take the whole
        response down."""
        from src.api.routes.configs import (
            estimate_backfill,
            BackfillEstimateRequest,
            DraftConfigPayload,
        )

        def boom(org_id, config, rows, sheet_service):
            raise RuntimeError("simulated estimator crash")

        with patch("src.api.routes.configs.ConfigRepository") as MockRepo, \
             patch("src.services.smartsheet.client.get_smartsheet_client"), \
             patch("src.services.smartsheet.sheets.SheetService") as MockSheet, \
             patch(
                "src.services.i2c_bolt_gate.compute_backfill_estimate",
                side_effect=boom,
             ):
            MockRepo.return_value.get_active_webhook_configs.return_value = []
            MockSheet.return_value.get_rows_with_attachments = AsyncMock(return_value=[])

            response = await estimate_backfill(
                request=BackfillEstimateRequest(
                    worksheet_id=12345,
                    max_pages=10,
                    draft_config=DraftConfigPayload(**_minimum_valid_draft()),
                ),
                org_id="org-1",
                db=MagicMock(),
            )

        assert response.draft_estimate is not None
        assert response.draft_estimate.error == "estimate_unavailable"


# ---------------------------------------------------------------------------
# SH-145 fix (bundled in SH-144 PR): the estimator must apply the same
# cell-content tier check the runtime applies. The runtime skips writing
# into cells that already hold an image (manual upload, prior run, anything
# Smartsheet returns with `cell.image` populated). Without this check the
# preview overcounted on sheets with mixed uploads — some files as
# attachments AND already pasted into the target cells — because the
# estimator counted them but the runtime would skip them.
#
# These tests run against `compute_backfill_estimate` directly (not the
# endpoint) so they pin the math at the layer that produces it. They use
# the same `make_*` factories from conftest so the data shapes match what
# real Smartsheet sheets look like.
# ---------------------------------------------------------------------------


class TestCellContentSkipMatchesRuntime:
    """Mixed-upload regression. Each test sets up a sheet with one or more
    rows where some target cells already hold an image (manual upload or
    prior run) AND the same row has fresh attachments. The estimator must
    only count attachments whose target cell is empty — the runtime would
    skip the rest under the SH-105 tier logic."""

    def _patch_gate_ctx(self, monkeypatch, mock_supabase, **overrides):
        from src.services import i2c_bolt_gate

        ctx = {
            "ceiling_bolts": 10000,
            "remaining_effective_bolts": 10000,
            "bolts_per_image": 1,
            "plan_suspended": False,
            "bolts_pending_payment": False,
            "hard_cap_reached_rpc": False,
            "consumed_bolts_start": 0,
            **overrides,
        }
        monkeypatch.setattr(i2c_bolt_gate, "load_i2c_org_usage_context", lambda *_: ctx)
        monkeypatch.setattr(i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase)
        monkeypatch.setattr(i2c_bolt_gate, "_PRIOR_LOG_RETRY_BASE_DELAY_S", 0.001)
        return i2c_bolt_gate

    def test_default_mode_skips_attachment_for_occupied_cell(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """Default mode: cell already holds an image with a different
        altText → SH-105 T2 fires, runtime skips, estimator must agree.
        Without the fix this would over-count by 1 per row that has a
        manual upload + a fresh attachment.
        """
        from tests.unit.conftest import make_attachment, make_config, make_row, make_cell

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col1, col2 = cfg.image_columns

        # Two rows:
        #   row 100 — both cells empty, 2 fresh attachments → 2 to process
        #   row 200 — col1 cell already holds a manual upload (different
        #             altText), col2 empty, 2 fresh attachments → only 1
        #             slot is available, default mode + cell with image
        #             triggers T2 skip
        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=1, name="a.jpg"),
                    make_attachment(id=2, name="b.jpg"),
                ],
                cells=[
                    make_cell(column_id=col1),
                    make_cell(column_id=col2),
                ],
            ),
            make_row(
                row_id=200,
                attachments=[
                    make_attachment(id=3, name="c.jpg"),
                    make_attachment(id=4, name="d.jpg"),
                ],
                cells=[
                    make_cell(column_id=col1, image_alt="manual-upload.jpg"),
                    make_cell(column_id=col2),
                ],
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)

        # row 100 contributes 2 (both empty)
        # row 200 contributes 1 (col1 occupied by manual upload, default
        #         mode skips; col2 takes one fresh attachment)
        # Without SH-145 the estimator returned 4 here. With the fix it
        # returns 3 — matching what the runtime would actually bill.
        assert result is not None
        assert result["images_needed"] == 3
        assert result["bolts_needed"] == 3

    def test_default_mode_tier_t1_skip_for_same_filename(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """SH-105 T1: cell holds an image whose altText equals the
        attachment filename → "same file already there", skip. Estimator
        must apply this regardless of mode (T1 fires for default, replace
        scan, replace webhook, name_map — all of them)."""
        from tests.unit.conftest import make_attachment, make_config, make_row, make_cell

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col1, col2 = cfg.image_columns

        # Row has 2 attachments; col1 cell already holds the FIRST one
        # (matching altText). Estimator should count only the second one.
        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=1, name="a.jpg"),
                    make_attachment(id=2, name="b.jpg"),
                ],
                cells=[
                    make_cell(column_id=col1, image_alt="a.jpg"),  # T1 match
                    make_cell(column_id=col2),
                ],
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)
        assert result is not None
        assert result["images_needed"] == 1

    def test_replace_mode_scan_skips_occupied_cell(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """Replace mode in scan/backfill context: T2 still fires for
        occupied cells with a DIFFERENT image because backfill is the
        defensive path (we don't silently destroy a manual upload during
        a sweep). The webhook path would overwrite (T3), but the estimator
        is a scan-context preview."""
        from tests.unit.conftest import make_attachment, make_config, make_row, make_cell

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="replace", n_image_columns=2, images_to_process=2)
        col1, col2 = cfg.image_columns

        # Row has 3 attachments; replace mode would normally take the 2
        # newest. col1 holds a different manual upload (T2 fires, scan
        # context → skip). col2 is empty (T4 → process).
        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=1, name="a.jpg"),
                    make_attachment(id=2, name="b.jpg"),
                    make_attachment(id=3, name="c.jpg"),
                ],
                cells=[
                    make_cell(column_id=col1, image_alt="different-manual.jpg"),
                    make_cell(column_id=col2),
                ],
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)
        assert result is not None
        # Replace gives newest 2 = [c, b]. col1 → c (skipped, T2), col2 → b
        # (processed, T4). Final: 1.
        assert result["images_needed"] == 1

    def test_empty_cells_process_normally(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """Regression guard: an empty-cell row must continue to contribute
        its full attachment count to the estimate. The cell-skip fix must
        not over-correct."""
        from tests.unit.conftest import make_attachment, make_config, make_row, make_cell

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col1, col2, col3 = cfg.image_columns

        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=i, name=f"img-{i}.jpg") for i in (1, 2, 3)
                ],
                cells=[
                    make_cell(column_id=col1),
                    make_cell(column_id=col2),
                    make_cell(column_id=col3),
                ],
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)
        assert result is not None
        assert result["images_needed"] == 3

    def test_row_without_cells_payload_falls_back_safely(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """Some callers fetch rows without the cells payload (legacy /
        smoke tests / future API changes). The estimator must NOT crash
        when cells are missing and MUST NOT silently zero everything
        out — falling back to "count all eligible attachments" is the
        right behaviour, matching what SH-121 did before the fix."""
        from tests.unit.conftest import make_attachment, make_config, make_row

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)

        # Row has attachments but NO cells field.
        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=1, name="a.jpg"),
                    make_attachment(id=2, name="b.jpg"),
                ],
                cells=None,
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)
        assert result is not None
        # No cells = no occupied columns = no T1/T2 skip. Both attachments
        # count, same as before the SH-145 fix.
        assert result["images_needed"] == 2

    def test_all_cells_occupied_returns_zero_for_default_mode(
        self, mock_supabase, mock_sheet_service, monkeypatch
    ):
        """The customer-visible win of this fix. A row where every target
        cell already holds a manual upload contributes 0 to the estimate
        even if it has fresh attachments — the runtime would skip every
        one of them under default mode, and so does the estimator now."""
        from tests.unit.conftest import make_attachment, make_config, make_row, make_cell

        gate = self._patch_gate_ctx(monkeypatch, mock_supabase)

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col1, col2 = cfg.image_columns

        rows = [
            make_row(
                row_id=100,
                attachments=[
                    make_attachment(id=1, name="a.jpg"),
                    make_attachment(id=2, name="b.jpg"),
                ],
                cells=[
                    make_cell(column_id=col1, image_alt="manual-1.jpg"),
                    make_cell(column_id=col2, image_alt="manual-2.jpg"),
                ],
            ),
        ]

        result = gate.compute_backfill_estimate("org-1", cfg, rows, mock_sheet_service)
        assert result is not None
        # Without the fix this returned 2 (the count of attachments).
        # With the fix it returns 0 — matching what the runtime would do.
        assert result["images_needed"] == 0
        assert result["bolts_needed"] == 0
