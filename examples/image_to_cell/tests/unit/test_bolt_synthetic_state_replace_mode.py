"""
Tests for REPLACE-mode billing semantics.

Commercial contract:
    - Replace places the newest N attachments into the target cells.
    - When the row is re-evaluated (eg. by a backfill that re-runs against
      the same sheet), only images that are TRULY NEW since the last
      processing are re-uploaded — and thus only those are billed. If no
      new attachments have appeared since the last processing, the row is
      a no-op: 0 bolts charged.

Implementation (after SH-75):
    - `ImageProcessor._build_synthetic_states_for_backfill` includes
      replace mode. During a backfill on rows with no DB state, the build
      populates state.last_ids and state.last_ts from prior usage_log
      entries that match the row's current attachments by filename.
    - `ReplaceMode.filter_attachments` returns the newest N MINUS any
      whose signature is already in state.last_ids — guaranteeing that
      a no-change re-run produces 0 bolts.

The pricing-side estimator in i2c_bolt_gate.preflight_backfill_budget
already handled replace correctly; estimator and runtime are now aligned.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_config, make_row


# `processor` fixture comes from conftest.py.


# ---------------------------------------------------------------------------
# Commercial contract — synthetic state + filter dedup
# ---------------------------------------------------------------------------


def test_replace_mode_synthetic_state_includes_prior_signatures(
    processor, mock_supabase, mock_sheet_service
):
    """A backfill row whose target columns already contain the same images
    produces a synthetic RowState with last_ids = those signatures, so
    ReplaceMode skips the no-op re-upload and bills 0 bolts.
    """
    cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
    col_ids = cfg.image_columns

    a1 = make_attachment(id=11, name="a.jpg")
    a2 = make_attachment(id=12, name="b.jpg")
    a3 = make_attachment(id=13, name="c.jpg")
    row = make_row(row_id=42, attachments=[a1, a2, a3])

    mock_supabase.usage_log_rows = [
        {"row_id": 42, "image_column_id": col_ids[0], "image_filename": "a.jpg"},
        {"row_id": 42, "image_column_id": col_ids[1], "image_filename": "b.jpg"},
        {"row_id": 42, "image_column_id": col_ids[2], "image_filename": "c.jpg"},
    ]

    synthetic = processor._build_synthetic_states_for_backfill(
        cfg, [row], mock_sheet_service
    )

    assert 42 in synthetic
    assert synthetic[42].last_ids == {a1.signature, a2.signature, a3.signature}


def test_replace_mode_filter_returns_newest_N_regardless_of_last_ids():
    """SH-111: ReplaceMode.filter_attachments ALWAYS returns the newest N
    attachments — `state.last_ids` is no longer consulted at the filter
    stage.

    Rationale: replace's invariant is "the N target columns always show
    the newest N attachments". When a new attachment arrives, the older
    ones must be re-placed in higher-indexed columns to preserve the
    order. The billing exemption (don't double-bill for shuffled
    attachments) now lives in `row_processor.process`, where signatures
    already present in `state.last_ids` before the run are excluded from
    `log_usage`. The cell upload still happens; only the bill is skipped.
    """
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState

    cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
    a1 = make_attachment(id=11, name="a.jpg")
    a2 = make_attachment(id=12, name="b.jpg")
    a3 = make_attachment(id=13, name="c.jpg")

    state = RowState(config_id=cfg.id, row_id=42)
    state.last_ids = {a1.signature, a2.signature, a3.signature}

    result = ReplaceMode().filter_attachments([a1, a2, a3], state, cfg)
    # Filter no longer subtracts last_ids — returns the newest 3 by ts.
    assert {a.signature for a in result} == {a1.signature, a2.signature, a3.signature}


def test_replace_mode_filter_returns_newest_N_even_when_some_in_last_ids():
    """SH-111 companion: even when only some attachments are in last_ids,
    the filter still returns the newest N. Billing skip is enforced
    downstream by row_processor."""
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState

    cfg = make_config(config_type="replace", n_image_columns=3, images_to_process=3)
    a1 = make_attachment(id=11, name="a.jpg")
    a2 = make_attachment(id=12, name="b.jpg")
    a3 = make_attachment(id=13, name="c.jpg")

    state = RowState(config_id=cfg.id, row_id=42)
    state.last_ids = {a3.signature, a2.signature}  # c and b already processed

    result = ReplaceMode().filter_attachments([a1, a2, a3], state, cfg)
    # Filter returns all newest 3, ordered newest-first.
    assert [a.signature for a in result] == [a3.signature, a2.signature, a1.signature]


# ---------------------------------------------------------------------------
# Behaviour that holds today — pin it so we don't regress on the way to a fix
# ---------------------------------------------------------------------------


def test_replace_mode_filter_returns_empty_when_no_attachments():
    """Empty input yields empty output regardless of state."""
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState

    cfg = make_config(config_type="replace", n_image_columns=2, images_to_process=2)
    state = RowState(config_id=cfg.id, row_id=1)

    assert ReplaceMode().filter_attachments([], state, cfg) == []


def test_replace_mode_filter_caps_at_images_to_process():
    """The filter must never return more than images_to_process attachments,
    even when many are available."""
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState

    cfg = make_config(config_type="replace", n_image_columns=2, images_to_process=2)
    atts = [make_attachment(id=i, name=f"img{i}.jpg") for i in range(1, 6)]
    state = RowState(config_id=cfg.id, row_id=1)

    result = ReplaceMode().filter_attachments(atts, state, cfg)
    assert len(result) == 2
