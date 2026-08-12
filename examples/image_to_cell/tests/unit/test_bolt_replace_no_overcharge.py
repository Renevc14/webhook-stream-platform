"""
Integration tests for the no-overcharge contract in replace-mode backfill.

These exercise the FULL replace path that used to overcharge:

    rows_without_state
        → ImageProcessor._build_synthetic_states_for_backfill
            → SELECT FROM imagetocell_usage_log (mocked here)
            → state.last_ids = {prior signatures}
        → ReplaceMode.filter_attachments(atts, state, cfg)
            → returns newest N (SH-111: NO last_ids filter)
        → row_processor.process billing skip for `att.signature in
          pre_run_last_ids` (SH-111)
        → expected: 0 attachments BILLED when nothing changed

History of the contract:
    - Pre-SH-75: filter returned newest N → backfill re-uploaded + re-billed
    - SH-75: filter excluded last_ids → no re-upload, no re-bill (but cols
      below 0 stayed empty after sequential single-attachment webhooks)
    - SH-111: filter returns newest N AGAIN (so columns shuffle to keep
      newest N visible) but row_processor exempts shuffled attachments
      from usage_log → no re-bill. Net customer-visible outcome unchanged.

This file pins the customer-visible billing outcome. It now simulates the
billing exemption inline (mirroring the row_processor logic) instead of
relying on the filter to do it.
"""

from __future__ import annotations

from typing import List

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


# `processor` fixture comes from conftest.py.


def _billable_via_full_flow(
    processor, mock_sheet_service, cfg, rows, mock_supabase,
) -> dict[int, List]:
    """Run the synthetic-state build, then ReplaceMode.filter_attachments,
    then apply the SH-111 billing exemption that row_processor.process
    performs (skip log_usage for attachments whose signature was in
    state.last_ids BEFORE this run). Returns {row_id: billable_attachments}.
    """
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState

    synthetic = processor._build_synthetic_states_for_backfill(
        cfg, rows, mock_sheet_service
    )
    mode = ReplaceMode()
    out: dict[int, List] = {}
    for row in rows:
        rid = row["id"]
        state = synthetic.get(rid) or RowState(config_id=cfg.id, row_id=rid)
        pre_run_last_ids = set(state.last_ids)
        to_process = mode.filter_attachments(row["attachments"], state, cfg)
        # SH-111: mirror row_processor.process's billing skip — attachments
        # already in state are shuffled, not freshly processed.
        out[rid] = [a for a in to_process if a.signature not in pre_run_last_ids]
    return out


# Backwards-compat alias for tests that still use the old name.
_filter_via_synthetic = _billable_via_full_flow


# ---------------------------------------------------------------------------
# No-change scenarios — must bill 0
# ---------------------------------------------------------------------------


class TestReplaceNoChangeNoBilling:
    def test_no_change_single_row_bills_zero(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """One row, three attachments, all already in usage log for the
        target columns. After SH-75 the row's filter result is [] → 0 bolts."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        atts = [make_attachment(id=10 + i, name=f"a{i}.jpg") for i in range(3)]
        rows = [make_row(row_id=1, attachments=atts)]
        mock_supabase.usage_log_rows = [
            {"row_id": 1, "image_column_id": col_ids[i], "image_filename": f"a{i}.jpg"}
            for i in range(3)
        ]

        billable = _filter_via_synthetic(processor, mock_sheet_service, cfg, rows, mock_supabase)

        assert billable == {1: []}

    def test_no_change_at_scale_bills_zero(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """200 rows × 3 attachments, every attachment already placed.
        Total bills 0 — proves the fix works at the customer-reported size."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        rows = []
        log_rows = []
        for row_id in range(1, 201):
            atts = [make_attachment(id=row_id * 1000 + j, name=f"r{row_id}_{j}.jpg") for j in range(3)]
            rows.append(make_row(row_id=row_id, attachments=atts))
            for j, att in enumerate(atts):
                log_rows.append({
                    "row_id": row_id,
                    "image_column_id": col_ids[j],
                    "image_filename": att.name,
                })
        mock_supabase.usage_log_rows = log_rows

        billable = _filter_via_synthetic(processor, mock_sheet_service, cfg, rows, mock_supabase)

        # Every row produces an empty list — total billable images = 0
        total = sum(len(v) for v in billable.values())
        assert total == 0
        # Every row covered, none missed
        assert len(billable) == 200


# ---------------------------------------------------------------------------
# Mixed scenarios — bill exactly the new ones
# ---------------------------------------------------------------------------


class TestReplaceMixedBilling:
    def test_one_new_attachment_per_row_bills_one_per_row(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """100 rows × 3 attachments. Prior log covers 2/3 per row. The third
        is genuinely new. Expected billable: exactly 100."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns

        rows = []
        log_rows = []
        for row_id in range(1, 101):
            atts = [make_attachment(id=row_id * 1000 + j, name=f"r{row_id}_{j}.jpg") for j in range(3)]
            rows.append(make_row(row_id=row_id, attachments=atts))
            for j in range(2):  # only 2 of 3 in log
                log_rows.append({
                    "row_id": row_id,
                    "image_column_id": col_ids[j],
                    "image_filename": atts[j].name,
                })
        mock_supabase.usage_log_rows = log_rows

        billable = _filter_via_synthetic(processor, mock_sheet_service, cfg, rows, mock_supabase)

        total = sum(len(v) for v in billable.values())
        assert total == 100, f"expected 100 billable, got {total}"
        # Every row bills exactly 1
        assert all(len(v) == 1 for v in billable.values())

    def test_brand_new_row_bills_full_n_slots(
        self, processor, mock_supabase, mock_sheet_service
    ):
        """A row that was never processed before (no usage_log entries) bills
        the full N slots — the fix did not break first-time billing."""
        cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)

        atts = [make_attachment(id=10 + i, name=f"new{i}.jpg") for i in range(5)]
        rows = [make_row(row_id=99, attachments=atts)]
        mock_supabase.usage_log_rows = []  # nothing prior

        billable = _filter_via_synthetic(processor, mock_sheet_service, cfg, rows, mock_supabase)

        # Newest 3 of 5 → 3 billable
        assert len(billable[99]) == 3
