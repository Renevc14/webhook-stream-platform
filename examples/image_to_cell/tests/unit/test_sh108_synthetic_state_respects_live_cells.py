"""
SH-108 tests — synthetic-state builder respects the live cell state.

The reported bug: a user processed attachments under config A, manually
cleared the destination cells, then deleted config A and created config B
pointing at the same columns. Re-running backfill under config B did NOT
refill the cleared cells because `_build_synthetic_states_for_backfill`
trusted `imagetocell_usage_log` blindly, locked the columns, and added the
prior attachment signatures to `last_ids` → `filter_attachments` returned
[] → silent skip.

The fix cross-checks each prior log entry against the live `cell.image`
data from the row payload. If the cell is currently empty, the entry is
dropped (no lock, no signature added), so the attachment is processed
again and the cell gets re-filled.

A safety net keeps the old behaviour when the response carries no cell
data at all — better to skip a row than to risk re-billing every prior
attachment on a partial response.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_cell, make_config, make_row


class TestSyntheticStateDropsPriorEntriesForEmptyCells:
    def test_cleared_cell_drops_prior_entry_so_attachment_reprocesses(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """User cleared col A after the original backfill. The new backfill
        must NOT see dog.5 as already processed — the cell needs to be
        refilled."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns

        att = make_attachment(id=500, name="dog.5.jpg")
        row = make_row(
            row_id=9001,
            attachments=[att],
            cells=[
                make_cell(column_id=col_a),  # cell empty (manually cleared)
                make_cell(column_id=col_b),  # cell empty
            ],
        )

        # Prior log says we processed dog.5 into col_a — but col_a is now empty
        mock_supabase.usage_log_rows = [
            {"row_id": 9001, "image_column_id": col_a, "image_filename": "dog.5.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        # No synthetic state should be emitted for this row: the only prior
        # entry was dropped because col_a is empty now, leaving last_ids empty.
        assert 9001 not in synthetic

    def test_cell_still_filled_preserves_prior_entry(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """Sanity check: when the cell still holds an image we keep trusting
        the prior log (existing dedup path remains intact)."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns

        att = make_attachment(id=500, name="dog.5.jpg")
        row = make_row(
            row_id=9002,
            attachments=[att],
            cells=[
                make_cell(column_id=col_a, image_alt="dog.5.jpg"),
                make_cell(column_id=col_b),
            ],
        )

        mock_supabase.usage_log_rows = [
            {"row_id": 9002, "image_column_id": col_a, "image_filename": "dog.5.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        assert 9002 in synthetic
        state = synthetic[9002]
        assert att.signature in state.last_ids
        assert 0 in state.locked_columns  # col_a index

    def test_mixed_cleared_and_filled_cells_keeps_only_filled(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """Two prior entries — one's cell still has an image, the other was
        cleared. Only the filled one survives in the synthetic state."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns

        att_a = make_attachment(id=600, name="dog.6.jpg")
        att_b = make_attachment(id=700, name="dog.7.jpg")
        row = make_row(
            row_id=9003,
            attachments=[att_a, att_b],
            cells=[
                make_cell(column_id=col_a),  # cleared
                make_cell(column_id=col_b, image_alt="dog.7.jpg"),  # still filled
            ],
        )

        mock_supabase.usage_log_rows = [
            {"row_id": 9003, "image_column_id": col_a, "image_filename": "dog.6.jpg"},
            {"row_id": 9003, "image_column_id": col_b, "image_filename": "dog.7.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        assert 9003 in synthetic
        state = synthetic[9003]
        # dog.7 (col_b) preserved, dog.6 (col_a) dropped
        assert att_b.signature in state.last_ids
        assert att_a.signature not in state.last_ids
        assert 1 in state.locked_columns      # col_b index locked
        assert 0 not in state.locked_columns  # col_a NOT locked → attachment can fill it

    def test_no_cells_in_payload_keeps_old_behavior(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """Safety net: if the row payload has no cells field at all we cannot
        verify, so we fall back to trusting the prior log (better to skip
        than risk re-billing on a partial response)."""
        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, _col_b = cfg.image_columns

        att = make_attachment(id=500, name="dog.5.jpg")
        # No cells key at all — make_row without cells= produces this.
        row = make_row(row_id=9004, attachments=[att])

        mock_supabase.usage_log_rows = [
            {"row_id": 9004, "image_column_id": col_a, "image_filename": "dog.5.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        # With no cells info we keep the prior entry → old (cautious) behaviour.
        assert 9004 in synthetic
        assert att.signature in synthetic[9004].last_ids

    def test_cleared_cell_with_no_matching_attachment_still_drops_silently(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """If the attachment file is no longer on the row AND the cell is
        empty, nothing to do — neither the lock nor the signature get added.
        The row produces no synthetic state."""
        cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
        col_a = cfg.image_columns[0]

        # No attachments at all on the row
        row = make_row(
            row_id=9005,
            attachments=[],
            cells=[make_cell(column_id=col_a)],  # empty
        )

        mock_supabase.usage_log_rows = [
            {"row_id": 9005, "image_column_id": col_a, "image_filename": "dog.5.jpg"},
        ]

        synthetic = processor._build_synthetic_states_for_backfill(
            cfg, [row], mock_sheet_service
        )

        assert 9005 not in synthetic
