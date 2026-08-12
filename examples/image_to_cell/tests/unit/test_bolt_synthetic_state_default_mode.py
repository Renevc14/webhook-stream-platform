"""
Tests for ImageProcessor._build_synthetic_states_for_backfill in DEFAULT mode.

This is the function that drives the user-reported bug: in large default-mode
backfills, all attachments get reprocessed instead of only the new ones.

Mechanics:
- During backfill, for each row that has no RowState in the DB yet, the
  processor queries `imagetocell_usage_log` to find images already placed
  in target columns by a prior config / prior backfill.
- For each match, it builds a synthetic RowState with:
    - last_ids   ← signatures of already-placed attachments
    - last_ts    ← max timestamp among them
    - locked_columns ← indices (in config.image_columns) of filled columns
- DefaultMode.filter_attachments uses (locked_columns, last_ids) to skip
  already-placed images.

Failure modes that produce the reported bug:
- The Supabase query fails → fail-open returns {} → every row treated as
  unprocessed → DefaultMode picks N attachments → all bolts charged again.
- Filename in usage_log does not match any attachment.name on the row →
  the entry is skipped silently → row treated as partially-processed but
  with the WRONG signatures.

These tests exercise both happy paths (so we are sure the matching logic
is correct) and failure modes (so we have a regression net for any future
fix).
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


# `processor` fixture comes from conftest.py — it patches both
# processor._db and i2c_bolt_gate.get_supabase_service to use mock_supabase.


# ---------------------------------------------------------------------------
# Happy path: synthetic state correctly identifies already-processed images
# ---------------------------------------------------------------------------


class TestDefaultModeHappyPath:
    def test_all_three_columns_already_filled_locks_all_three(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """Row already has 3 attachments placed in 3 target columns. Synthetic
        state must lock indices 0, 1, 2 and add all 3 signatures to last_ids
        — so DefaultMode treats the row as fully processed and skips it."""
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        a1 = make_attachment(id=101, name="img1.jpg")
        a2 = make_attachment(id=102, name="img2.jpg")
        a3 = make_attachment(id=103, name="img3.jpg")
        row = make_row(row_id=5001, attachments=[a1, a2, a3])

        mock_supabase.usage_log_rows = [
            {"row_id": 5001, "image_column_id": col_ids[0], "image_filename": "img1.jpg"},
            {"row_id": 5001, "image_column_id": col_ids[1], "image_filename": "img2.jpg"},
            {"row_id": 5001, "image_column_id": col_ids[2], "image_filename": "img3.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        assert 5001 in synthetic, "expected synthetic state for row 5001"
        state = synthetic[5001]
        assert sorted(state.locked_columns) == [0, 1, 2]
        assert state.last_ids == {a1.signature, a2.signature, a3.signature}
        assert state.last_ts == max(
            a1.created_timestamp, a2.created_timestamp, a3.created_timestamp
        )

    def test_partial_fill_locks_only_filled_indices(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """Row has 5 attachments but only 2 are already placed (indices 0, 1).
        Synthetic state should lock only those two, leaving 1 slot free for
        DefaultMode to fill (images_to_process=3)."""
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        atts = [make_attachment(id=200 + i, name=f"img{i}.jpg") for i in range(5)]
        row = make_row(row_id=5002, attachments=atts)

        mock_supabase.usage_log_rows = [
            {"row_id": 5002, "image_column_id": col_ids[0], "image_filename": "img0.jpg"},
            {"row_id": 5002, "image_column_id": col_ids[1], "image_filename": "img1.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        state = synthetic[5002]
        assert sorted(state.locked_columns) == [0, 1]
        assert state.last_ids == {atts[0].signature, atts[1].signature}

    def test_multiple_rows_each_get_correct_synthetic_state(
        self, processor, mock_supabase, mock_sheet_service
    ):
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_ids = cfg.image_columns

        # Row A: both columns filled
        row_a_atts = [make_attachment(id=10, name="A1.jpg"), make_attachment(id=11, name="A2.jpg")]
        # Row B: only column 0 filled
        row_b_atts = [make_attachment(id=20, name="B1.jpg"), make_attachment(id=21, name="B2.jpg")]
        # Row C: nothing in usage log → no entry in synthetic
        row_c_atts = [make_attachment(id=30, name="C1.jpg")]

        mock_supabase.usage_log_rows = [
            {"row_id": 100, "image_column_id": col_ids[0], "image_filename": "A1.jpg"},
            {"row_id": 100, "image_column_id": col_ids[1], "image_filename": "A2.jpg"},
            {"row_id": 200, "image_column_id": col_ids[0], "image_filename": "B1.jpg"},
        ]

        rows = [
            make_row(row_id=100, attachments=row_a_atts),
            make_row(row_id=200, attachments=row_b_atts),
            make_row(row_id=300, attachments=row_c_atts),
        ]

        synthetic = processor._build_synthetic_states_for_backfill(cfg, rows, mock_sheet_service)

        assert sorted(synthetic.keys()) == [100, 200], "row 300 has no prior log → no entry"
        assert sorted(synthetic[100].locked_columns) == [0, 1]
        assert sorted(synthetic[200].locked_columns) == [0]


# ---------------------------------------------------------------------------
# Edge cases that already work (defensive guards)
# ---------------------------------------------------------------------------


class TestDefaultModeEdgeCases:
    def test_empty_rows_list_returns_empty_dict(
        self, processor, mock_supabase, mock_sheet_service
    ):
        cfg = make_config(config_type="default", n_image_columns=3)
        synthetic = processor._build_synthetic_states_for_backfill(cfg, [], mock_sheet_service)
        assert synthetic == {}

    def test_config_with_no_image_columns_returns_empty(
        self, processor, mock_supabase, mock_sheet_service
    ):
        from src.models.config import ImageToCellConfig

        cfg = ImageToCellConfig(
            id="cfg",
            organization_id="org",
            worksheet_id=1,
            configuration_name="empty cols",
            config_type="default",
            images_to_process=1,
        )
        row = make_row(row_id=1, attachments=[make_attachment(id=1, name="x.jpg")])
        synthetic = processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)
        assert synthetic == {}

    def test_usage_log_empty_returns_empty(
        self, processor, mock_supabase, mock_sheet_service
    ):
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        row = make_row(row_id=1, attachments=[make_attachment(id=1, name="x.jpg")])
        mock_supabase.usage_log_rows = []
        synthetic = processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)
        assert synthetic == {}


# ---------------------------------------------------------------------------
# Bug repro — failure modes that cause the reported behaviour
# ---------------------------------------------------------------------------


class TestDefaultModePriorLogUnavailable:
    """When the prior-processed lookup fails after retries, the synthetic
    state build raises PriorLogUnavailable so the caller can abort the
    backfill cleanly. Silent fail-open is no longer acceptable — that
    path was responsible for the customer-reported overcharge in
    backfills run during transient Supabase connection loss."""

    def test_supabase_failure_raises_PriorLogUnavailable(
        self, processor, mock_supabase, mock_sheet_service
    ):
        from src.services.i2c_bolt_gate import PriorLogUnavailable

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        atts = [make_attachment(id=400 + i, name=f"img{i}.jpg") for i in range(3)]
        row = make_row(row_id=9001, attachments=atts)

        mock_supabase.usage_log_error = RuntimeError(
            "Server disconnected without sending a response"
        )

        with pytest.raises(PriorLogUnavailable, match="imagetocell_usage_log"):
            processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)


class TestDefaultModeBugRepro:

    def test_filename_mismatch_yields_no_locked_columns(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """If usage_log records a filename that no longer appears on the row
        (for instance, the user deleted and re-uploaded the attachment with
        a new name), there is nothing to lock. The row is treated as
        unprocessed — by design, since the attachments truly are new."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_ids = cfg.image_columns

        # Current attachments on the row
        atts = [
            make_attachment(id=500, name="newname-1.jpg"),
            make_attachment(id=501, name="newname-2.jpg"),
        ]
        row = make_row(row_id=7001, attachments=atts)

        # Usage log has DIFFERENT filenames (eg. older deleted versions)
        mock_supabase.usage_log_rows = [
            {"row_id": 7001, "image_column_id": col_ids[0], "image_filename": "deleted-old-1.jpg"},
            {"row_id": 7001, "image_column_id": col_ids[1], "image_filename": "deleted-old-2.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)

        # No matching attachments → no lockable columns → no synthetic state
        assert 7001 not in synthetic

    def test_partial_filename_match_only_locks_matched_indices(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """If 1 of 2 logged filenames matches a current attachment, only that
        column is locked. The other slot is considered free to fill."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_ids = cfg.image_columns

        atts = [
            make_attachment(id=600, name="kept.jpg"),
            make_attachment(id=601, name="kept-2.jpg"),
        ]
        row = make_row(row_id=7100, attachments=atts)

        mock_supabase.usage_log_rows = [
            {"row_id": 7100, "image_column_id": col_ids[0], "image_filename": "kept.jpg"},
            {"row_id": 7100, "image_column_id": col_ids[1], "image_filename": "deleted.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(cfg, [row], mock_sheet_service)

        state = synthetic[7100]
        assert state.locked_columns == [0]
        assert state.last_ids == {atts[0].signature}
