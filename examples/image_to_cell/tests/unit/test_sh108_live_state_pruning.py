"""
SH-108 live-path tests — RowProcessor._prune_stale_state.

Companion to test_sh108_synthetic_state_respects_live_cells.py. That suite
covers the backfill BUILD path (synthetic state assembled from
imagetocell_usage_log); this suite covers the LIVE path that runs on every
RowProcessor.process call, hitting BOTH:

  - Webhook events (process_row reads persisted state from
    imagetocell_row_state)
  - Backfill rows that already have a row_state entry (the synthetic
    builder skips those — we need to prune them here instead).

Bug shape addressed:
  - User runs backfill → cells filled → state persisted with
    locked_columns + last_ids
  - User manually clears the cells
  - User uploads a new attachment OR re-triggers backfill on the same
    config
  - Without this fix:
      * default.should_process_row returns False because
        len(state.locked_columns) >= n_slots (SH-113 changed this from
        state.processed_count; SH-108's prune unlocks the column so the
        check now passes)
      * even if it did process, default.get_target_columns would exclude
        every locked column → no target for the new attachment
"""

from __future__ import annotations

from typing import List

import pytest

from tests.unit.conftest import make_attachment, make_cell, make_config


def _build_state(*, config, attachments_in_last_ids: List, locked_indices: List[int]):
    from src.models.row_state import RowState

    state = RowState(config_id=config.id, row_id=999)
    for att in attachments_in_last_ids:
        state.last_ids.add(att.signature)
    for idx in locked_indices:
        state.lock_column(idx)
    return state


class TestPruneStaleState:
    def test_drops_locked_columns_whose_cells_are_empty(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[0, 1],
        )
        # Both cells cleared
        cell_images = {}
        columns_with_images = set()
        attachments = [att_x]  # still on row

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert state.locked_columns == [], "both locks must be dropped"

    def test_keeps_locked_columns_whose_cells_still_have_image(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[0, 1],
        )
        # col_a still has image, col_b cleared
        cell_images = {col_a: {"id": "IMG-A", "altText": "x.jpg"}}
        columns_with_images = {col_a}
        attachments = [att_x]

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert state.locked_columns == [0], "only col_b (idx 1) should be unlocked"

    def test_drops_signature_when_attachment_on_row_but_cell_empty(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[],
        )
        cell_images = {}  # no cell has any image
        columns_with_images = set()
        attachments = [att_x]  # still on row

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert state.last_ids == set(), "signature must be dropped — no cell carries x.jpg"

    def test_keeps_signature_when_cell_still_has_matching_altText(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, _ = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[0],
        )
        cell_images = {col_a: {"id": "IMG-A", "altText": "x.jpg"}}
        columns_with_images = {col_a}
        attachments = [att_x]

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert att_x.signature in state.last_ids, "signature must be preserved"
        assert state.locked_columns == [0], "lock must be preserved"

    def test_keeps_signature_when_attachment_no_longer_on_row(self):
        """Defensive: attachment was removed by the user. We don't touch
        the signature — removing it could cause re-billing if the
        attachment briefly disappeared from a partial response."""
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[],
        )
        cell_images = {}
        columns_with_images = set()
        attachments = []  # attachment is gone from the row

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert att_x.signature in state.last_ids

    def test_mixed_cleanup_partial_clear(self):
        """One column cleared, the other still filled. Lock for the cleared
        col drops; lock for the filled col stays. Signature for the
        attachment whose cell was cleared drops; the other survives."""
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")   # was in col_a (cleared)
        att_y = make_attachment(id=2, name="y.jpg")   # was in col_b (still there)

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x, att_y],
            locked_indices=[0, 1],
        )
        cell_images = {col_b: {"id": "IMG-B", "altText": "y.jpg"}}
        columns_with_images = {col_b}
        attachments = [att_x, att_y]

        RowProcessor._prune_stale_state(
            state, attachments, cell_images, columns_with_images, cfg, row_id=999
        )

        assert state.locked_columns == [1]
        assert att_x.signature not in state.last_ids
        assert att_y.signature in state.last_ids

    def test_is_a_noop_when_state_is_clean(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, _ = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x],
            locked_indices=[0],
        )
        cell_images = {col_a: {"id": "IMG-A", "altText": "x.jpg"}}

        before_locked = list(state.locked_columns)
        before_sigs = set(state.last_ids)

        RowProcessor._prune_stale_state(
            state, [att_x], cell_images, {col_a}, cfg, row_id=999
        )

        assert list(state.locked_columns) == before_locked
        assert set(state.last_ids) == before_sigs

    def test_resets_last_ts_when_stale_signatures_dropped(self):
        """Cleared cells must not leave a stale last_ts watermark.

        Staging SH-183: prune dropped last_ids but last_ts stayed high, so
        default.should_process_row skipped every row as 'no new attachments'.
        """
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        att_x = make_attachment(id=1, name="x.jpg")
        att_y = make_attachment(id=2, name="y.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x, att_y],
            locked_indices=[0, 1],
        )
        state.last_ts = max(att_x.created_timestamp, att_y.created_timestamp)

        RowProcessor._prune_stale_state(
            state, [att_x, att_y], {}, set(), cfg, row_id=999
        )

        assert state.last_ids == set()
        assert state.last_ts == 0.0

    def test_recomputes_last_ts_from_remaining_filled_signatures(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns
        att_x = make_attachment(id=1, name="x.jpg")
        att_y = make_attachment(id=2, name="y.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x, att_y],
            locked_indices=[0, 1],
        )
        state.last_ts = att_y.created_timestamp

        # col_a cleared (x), col_b still filled (y)
        cell_images = {col_b: {"id": "IMG-B", "altText": "y.jpg"}}
        RowProcessor._prune_stale_state(
            state, [att_x, att_y], cell_images, {col_b}, cfg, row_id=999
        )

        assert att_x.signature not in state.last_ids
        assert att_y.signature in state.last_ids
        assert state.last_ts == att_y.created_timestamp


class TestDefaultShouldProcessAfterClearedCells:
    """Estimate/runtime parity: after SH-108 prune, default mode must still
    see open-slot refill work even when last_ts is stale."""

    def test_should_process_unprocessed_despite_stale_last_ts(self):
        from src.core.modes.default import DefaultMode
        from src.core.row_processor import RowProcessor

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        att_x = make_attachment(id=10, name="horse.jpg")
        att_y = make_attachment(id=11, name="dog.jpg")

        state = _build_state(
            config=cfg,
            attachments_in_last_ids=[att_x, att_y],
            locked_indices=[0, 1],
        )
        # Watermark from the prior successful fill — both attachments are older.
        state.last_ts = max(att_x.created_timestamp, att_y.created_timestamp) + 1000

        RowProcessor._prune_stale_state(
            state, [att_x, att_y], {}, set(), cfg, row_id=999
        )

        mode = DefaultMode()
        assert mode.should_process_row([att_x, att_y], state, cfg) is True
        eligible = mode.filter_attachments([att_x, att_y], state, cfg)
        assert {a.name for a in eligible} == {"horse.jpg", "dog.jpg"}


# ---------------------------------------------------------------------------
# End-to-end check that the pruning is invoked during process()
# ---------------------------------------------------------------------------


class TestProcessInvokesPruning:
    """Smoke test: verify RowProcessor.process actually calls _prune_stale_state
    when a row arrives with cell.image data + a state that has stale entries.
    Patches _prune_stale_state to a spy and asserts the call shape."""

    def test_process_calls_prune_when_state_has_entries_and_row_has_cells(
        self, monkeypatch
    ):
        from src.core.row_processor import RowProcessor
        from src.models.row_state import RowState

        # Build a RowProcessor with stubbed dependencies — process() returns
        # early in most cases; we only need _prune_stale_state to be hit.
        rp = RowProcessor.__new__(RowProcessor)
        rp._ss = None
        rp._state_repo = type("R", (), {
            "get": lambda self, *a, **k: None,
            "save": lambda self, *a, **k: None,
        })()
        rp._settings = type("S", (), {"enable_parallel_image_processing": False})()
        rp._sheet_service = type("SS", (), {
            "extract_row_attachments": staticmethod(
                lambda row, images_only=True: list(row.get("attachments", []))
            )
        })()
        rp._image_handler = None
        # SH-176 moved `_modes` from class level to __init__ (NameMapMode now
        # takes max_upload_attempts), so a __new__-built instance must set it.
        from src.core.modes import NameMapMode
        rp._modes = {
            **{name: cls() for name, cls in RowProcessor._mode_classes.items()},
            "name map": NameMapMode(max_upload_attempts=5),
        }

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, _col_b = cfg.image_columns
        att = make_attachment(id=1, name="x.jpg")
        state = RowState(config_id=cfg.id, row_id=999)
        state.last_ids.add(att.signature)
        state.lock_column(0)

        row = {
            "id": 999,
            "attachments": [att],
            "cells": [make_cell(column_id=col_a)],  # cleared
        }

        calls = []
        original = RowProcessor._prune_stale_state
        def spy(state_arg, attachments_arg, cell_images_arg, cols_arg, config_arg, row_id_arg):
            calls.append(row_id_arg)
            return original(state_arg, attachments_arg, cell_images_arg, cols_arg, config_arg, row_id_arg)
        monkeypatch.setattr(RowProcessor, "_prune_stale_state", staticmethod(spy))

        # Disable bolt-cap shortcut
        import src.core.row_processor as rpmod
        monkeypatch.setattr(rpmod, "get_remaining_image_capacity", None)
        monkeypatch.setattr(rpmod, "log_usage", lambda **kwargs: None)

        try:
            # SH-101: rp.process is async-first.
            import asyncio
            asyncio.run(
                rp.process(cfg, row, state=state, is_scan_context=True)
            )
        except Exception:
            # We don't care if downstream image processing fails — the
            # check we want is that the prune ran first.
            pass

        assert calls == [999], "prune must be called once with the row_id"
        # And the prune should have actually cleaned the state
        assert state.locked_columns == [], "stale lock should be removed"
        assert state.last_ids == set(), "stale signature should be removed"
