"""
Tests for ImageProcessor._build_synthetic_states_for_backfill in NAME_MAP
mode, plus NameMapMode billing semantics that depend on the synthetic state.

Commercial contract:
    - Each attachment is matched to a column by filename pattern.
    - Once placed, it is tracked (signature in last_ids, max timestamp in
      last_ts) so a re-run does not re-upload it.
    - A backfill on a row whose target columns already contain the same
      attachments must charge 0 bolts. If a new matching attachment has
      been added since, only that new one is billed.

Synthetic-state contract for NameMap:
    - Like default mode, the build queries imagetocell_usage_log filtered
      to the config's target columns and the row IDs in question.
    - For each match it adds the signature to last_ids and bumps last_ts.
    - It also calls lock_column(idx) for the matched column id, but
      NameMapMode.filter_attachments does not consult locked_columns —
      its dedup is signature-based via get_unprocessed_attachments. So locking
      is harmless but irrelevant for name_map.

Bug shared with default mode:
    - When the Supabase query fails the build returns {} (fail-open) and
      every row appears to have no prior state. NameMapMode then processes
      every name-matching attachment and bills the customer for work that
      already happened — same overcharge pattern as the reported default
      mode bug.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


# `processor` fixture comes from conftest.py.


# ---------------------------------------------------------------------------
# Synthetic state — happy path
# ---------------------------------------------------------------------------


class TestNameMapSyntheticState:
    def test_prior_log_populates_last_ids_and_last_ts(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """The synthetic state for a name_map row with prior log entries
        contains every matched signature plus the max timestamp."""
        cfg = make_config(
            config_type="name map",
            n_image_columns=3,
            images_to_process=3,
            column_name_prefix="part",
        )
        col_ids = cfg.image_columns

        a1 = make_attachment(id=11, name="part-1.jpg")
        a2 = make_attachment(id=12, name="part-2.jpg")
        a3 = make_attachment(id=13, name="part-3.jpg")
        row = make_row(row_id=80, attachments=[a1, a2, a3])

        mock_supabase.usage_log_rows = [
            {"row_id": 80, "image_column_id": col_ids[0], "image_filename": "part-1.jpg"},
            {"row_id": 80, "image_column_id": col_ids[1], "image_filename": "part-2.jpg"},
            {"row_id": 80, "image_column_id": col_ids[2], "image_filename": "part-3.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        state = synthetic[80]
        assert state.last_ids == {a1.signature, a2.signature, a3.signature}
        assert state.last_ts == max(
            a1.created_timestamp, a2.created_timestamp, a3.created_timestamp
        )

    def test_partial_match_only_records_matched_signatures(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """When usage_log mentions a filename no longer present on the row
        (deleted attachment), only the still-present matches are recorded."""
        cfg = make_config(config_type="name map", n_image_columns=2, images_to_process=2)
        col_ids = cfg.image_columns

        a_kept = make_attachment(id=21, name="kept.jpg")
        # The other attachment was deleted; only one filename matches a log entry
        row = make_row(row_id=81, attachments=[a_kept])

        mock_supabase.usage_log_rows = [
            {"row_id": 81, "image_column_id": col_ids[0], "image_filename": "kept.jpg"},
            {"row_id": 81, "image_column_id": col_ids[1], "image_filename": "deleted.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        state = synthetic[81]
        assert state.last_ids == {a_kept.signature}
        # last_ts should be the kept attachment's timestamp, not zero
        assert state.last_ts == a_kept.created_timestamp


# ---------------------------------------------------------------------------
# NameMapMode billing semantics
# ---------------------------------------------------------------------------


class TestNameMapBillingSemantics:
    def test_filter_returns_empty_when_all_signatures_already_processed(self):
        """Once every matched attachment signature is in last_ids, nothing
        is pending and filter returns []. 0 bolts charged."""
        from src.core.modes.name_map import NameMapMode
        from src.models.row_state import RowState

        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            images_to_process=2,
            column_name_prefix="part",
        )
        a1 = make_attachment(id=1, name="part-1.jpg")
        a2 = make_attachment(id=2, name="part-2.jpg")

        state = RowState(config_id=cfg.id, row_id=80)
        state.last_ids.update({a1.signature, a2.signature})

        result = NameMapMode().filter_attachments([a1, a2], state, cfg)
        assert result == [], "all signatures already processed → 0 bolts"

    def test_filter_returns_only_unprocessed_signatures(self):
        """If one attachment was already placed, only the other is returned."""
        from src.core.modes.name_map import NameMapMode
        from src.models.row_state import RowState

        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            images_to_process=2,
            column_name_prefix="part",
        )
        a_old = make_attachment(id=1, name="part-1.jpg")
        a_new = make_attachment(id=99, name="part-2.jpg")

        state = RowState(config_id=cfg.id, row_id=80)
        state.last_ids.add(a_old.signature)

        result = NameMapMode().filter_attachments([a_old, a_new], state, cfg)
        assert result == [a_new]

    def test_filter_excludes_attachments_with_no_matching_column(self):
        """Attachments whose names do not match any configured column name
        must never be processed — even when they are not yet in last_ids."""
        from src.core.modes.name_map import NameMapMode
        from src.models.row_state import RowState

        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            images_to_process=2,
            column_name_prefix="part",
        )
        # Names that don't match "part-1" or "part-2"
        random_1 = make_attachment(id=50, name="screenshot.jpg")
        random_2 = make_attachment(id=51, name="receipt.jpg")

        state = RowState(config_id=cfg.id, row_id=80)

        result = NameMapMode().filter_attachments([random_1, random_2], state, cfg)
        assert result == [], "no name match → 0 bolts even though atts are 'new'"


# ---------------------------------------------------------------------------
# Bug shared with default mode — fail-open on Supabase failure
# ---------------------------------------------------------------------------


class TestNameMapPriorLogUnavailable:
    """Same fix as default mode — when the prior log lookup fails after
    retries, synthetic state build raises so the caller can abort cleanly.
    Pinned per mode because future regressions may apply differently."""

    def test_supabase_failure_raises_PriorLogUnavailable(
        self, processor, mock_supabase, mock_sheet_service
    ):
        from src.services.i2c_bolt_gate import PriorLogUnavailable

        cfg = make_config(config_type="name map", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=400 + i, name=f"part-{i}.jpg") for i in range(3)]
        row = make_row(row_id=9001, attachments=atts)

        mock_supabase.usage_log_error = RuntimeError(
            "Server disconnected without sending a response"
        )

        with pytest.raises(PriorLogUnavailable, match="imagetocell_usage_log"):
            processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)
