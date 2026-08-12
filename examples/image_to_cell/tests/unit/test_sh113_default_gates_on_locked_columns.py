"""
SH-113 tests — default.should_process_row gates on
`len(state.locked_columns) >= n_slots`, not `state.processed_count`.

Bug shape:
  - Backfill fills 2 cells of a 2-slot config. state.last_ids = {sig_a, sig_b},
    state.locked_columns = [0, 1].
  - User manually deletes the image from col 0 AND removes the original
    attachment from the row.
  - User uploads a new attachment that should land in the now-empty col 0.
  - SH-108._prune_stale_state correctly unlocks col 0 (cell empty),
    BUT keeps sig_a in last_ids (defensive — the attachment is gone, removing
    it could re-bill if the attachment briefly disappeared from a partial
    response).
  - Pre-SH-113: default.should_process_row used `state.processed_count`
    (== len(last_ids)). After pruning, last_ids still has 2 entries, so
    processed_count=2 >= n_slots=2 → SKIP. The new attachment never gets
    processed. ← the bug observed in logs_default.txt at 12:40 on row
    3920632550981508.
  - Post-SH-113: the check uses len(state.locked_columns). After pruning,
    locked_columns=[1] (1 < 2) → process. The new attachment lands in col 0.

The "billing dedup" role of last_ids is unaffected: the new attachment
has a fresh signature, so log_usage proceeds. Only the "row is full"
gating decision was wrong.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_config


def _state_with(*, config, locked_indices, last_ids_signatures):
    from src.models.row_state import RowState

    state = RowState(config_id=config.id, row_id=12345)
    state.last_ids.update(last_ids_signatures)
    for idx in locked_indices:
        state.lock_column(idx)
    return state


class TestDefaultGatesOnLockedColumns:
    def test_row_with_one_locked_slot_processes_new_attachment(self):
        """The exact logs_default.txt scenario: 2-slot row, col 0 unlocked
        by SH-108 prune (cell empty + attachment removed), last_ids still
        has 2 stale signatures. The new attachment must process."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        # locked_columns = [1] (only col 1 still has an image after SH-108 prune)
        # last_ids has 2 entries (defensive: original attachments removed)
        state = _state_with(
            config=cfg,
            locked_indices=[1],
            last_ids_signatures={"stale-sig-1", "stale-sig-2"},
        )
        new_att = make_attachment(id=99, name="dog.99.jpg")

        assert DefaultMode().should_process_row([new_att], state, cfg) is True

    def test_row_with_all_slots_locked_still_skips(self):
        """Regression: when locked_columns IS full, we still skip. SH-113
        only changes WHICH counter is consulted, not the skip behaviour
        when the row is truly full."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        state = _state_with(
            config=cfg,
            locked_indices=[0, 1],
            last_ids_signatures={"sig-1", "sig-2"},
        )
        att = make_attachment(id=99, name="new.jpg")

        assert DefaultMode().should_process_row([att], state, cfg) is False

    def test_high_last_ids_count_does_not_block_when_locks_below_n_slots(self):
        """Edge case: last_ids has many entries (e.g., a long history of
        manually cleared+re-filled cycles where the defensive logic
        accumulated stale sigs), but locked_columns is well under n_slots.
        The row must process — last_ids count is now irrelevant to gating."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        state = _state_with(
            config=cfg,
            locked_indices=[0],  # only 1 slot locked
            last_ids_signatures={f"stale-{i}" for i in range(20)},  # 20 stale sigs
        )
        att = make_attachment(id=99, name="fresh.jpg")

        assert DefaultMode().should_process_row([att], state, cfg) is True

    def test_empty_locked_columns_processes(self):
        """Fresh row with no locks (first webhook on the row). Trivial
        case but worth pinning down: locked_columns=[] means 0 < n_slots,
        always process."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        state = _state_with(
            config=cfg,
            locked_indices=[],
            last_ids_signatures=set(),
        )
        att = make_attachment(id=1, name="first.jpg")

        assert DefaultMode().should_process_row([att], state, cfg) is True

    def test_images_to_process_less_than_image_columns(self):
        """images_to_process=1 with 3 configured image columns. Once 1
        slot is locked the row is "full" per the config's intent."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(
            config_type="default",
            n_image_columns=3,
            images_to_process=1,
        )
        state = _state_with(
            config=cfg,
            locked_indices=[0],
            last_ids_signatures={"sig"},
        )
        att = make_attachment(id=99, name="extra.jpg")

        assert DefaultMode().should_process_row([att], state, cfg) is False

    def test_no_attachments_still_skips(self):
        """Regression on the earliest check: empty attachments returns
        False regardless of state."""
        from src.core.modes.default import DefaultMode

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        state = _state_with(
            config=cfg,
            locked_indices=[],
            last_ids_signatures=set(),
        )

        assert DefaultMode().should_process_row([], state, cfg) is False


# ---------------------------------------------------------------------------
# Integration with SH-108: end-to-end shape of the bug + fix
# ---------------------------------------------------------------------------


class TestSh108PruneFollowedBySh114Gate:
    """SH-108 prunes locked_columns based on live cell.image; SH-113
    consults the pruned locked_columns count. These tests pin down the
    interaction so a future refactor doesn't break either half."""

    def test_prune_then_gate_allows_new_attachment(self):
        """After prune unlocks col 0, the gate sees 1 < 2 and allows
        processing. last_ids is preserved for billing dedup of any
        attachment whose signature is still present."""
        from src.core.modes.default import DefaultMode
        from src.core.row_processor import RowProcessor
        from src.models.row_state import RowState

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, col_b = cfg.image_columns

        # Initial state: both cells were filled and locked, last_ids has
        # the two original signatures.
        state = RowState(config_id=cfg.id, row_id=999)
        state.last_ids.update({"orig-sig-a", "orig-sig-b"})
        state.lock_column(0)
        state.lock_column(1)

        # Now col_a is empty (user cleared); col_b still has its image.
        # Both original attachments were removed from the row (so the
        # defensive branch of prune keeps the signatures).
        cell_images = {col_b: {"id": "IMG-B", "altText": "kept.jpg"}}
        columns_with_images = {col_b}
        new_attachment = make_attachment(id=99, name="new.jpg")
        attachments_on_row = [new_attachment]  # only the new one

        RowProcessor._prune_stale_state(
            state, attachments_on_row, cell_images, columns_with_images, cfg, row_id=999
        )

        # Prune should have unlocked col 0 (idx 0) and kept the defensive
        # signatures.
        assert state.locked_columns == [1]
        assert state.last_ids == {"orig-sig-a", "orig-sig-b"}

        # Now the SH-113 gate must allow processing.
        assert DefaultMode().should_process_row(
            attachments_on_row, state, cfg
        ) is True
