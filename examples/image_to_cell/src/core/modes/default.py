"""
Default Processing Mode
=======================

Fill N image slots once, then lock the row.
"""

import logging
from typing import List, Dict, Optional, Set

from src.models.config import ImageToCellConfig
from src.models.attachment import Attachment
from src.models.row_state import RowState
from src.core.modes.base import BaseMode


log = logging.getLogger(__name__)


class DefaultMode(BaseMode):
    """
    Default processing mode.

    Behavior:
    - Takes the first N images (oldest first)
    - Places them in the configured columns
    - Once all N slots are filled, the row is "locked"
    - Locked rows are never processed again
    """

    @property
    def name(self) -> str:
        return "default"

    def should_process_row(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> bool:
        """
        Check if row should be processed.

        Row should be processed if:
        - There are attachments
        - Not all slots are filled (row not locked)
        - There are new attachments since last processing
        """
        if not attachments:
            log.info(
                "[default.should_process_row] row=%s skip — no processable attachments",
                state.row_id,
            )
            return False

        n_slots = config.images_to_process

        # SH-113: gate on locked_columns, not last_ids count. last_ids is the
        # billing-dedup ledger and may legitimately retain signatures of
        # attachments whose backing cell has since been cleared (SH-108's
        # prune keeps those entries when the attachment itself was removed
        # from the row, to avoid re-billing on a partial-response flicker).
        # Using len(state.last_ids) here conflates "row is full" with "we
        # have billed for N images at some point", which made manually-
        # cleared cells un-processable. locked_columns is reconciled against
        # live cell.image by SH-108's _prune_stale_state, so it reflects the
        # true count of cells currently holding an image we placed.
        if len(state.locked_columns) >= n_slots:
            log.info(
                "[default.should_process_row] row=%s skip — row locked, all %d slots filled (locked=%d, last_ids=%d)",
                state.row_id, n_slots, len(state.locked_columns), len(state.last_ids),
            )
            return False

        # Prefer signature ledger over last_ts. After SH-108 clears cells,
        # _prune_stale_state drops those signatures from last_ids but a stale
        # last_ts watermark would make get_new_attachments() return [] and
        # skip the refill — estimate counts the work (fresh state, last_ts=0)
        # while runtime no-ops. Unprocessed signatures with open slots are
        # always work; last_ts only gates brand-new uploads when everything
        # currently on the row is already in last_ids.
        unprocessed = [a for a in attachments if a.signature not in state.last_ids]
        if unprocessed:
            return True

        new_attachments = self.get_new_attachments(attachments, state)
        if not new_attachments:
            log.info(
                "[default.should_process_row] row=%s skip — no new attachments since last_ts=%s (have %d, %d processed)",
                state.row_id, state.last_ts, len(attachments), len(state.last_ids),
            )
            return False

        return True

    def filter_attachments(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> List[Attachment]:
        """
        Get attachments to process: oldest first, excluding already-processed
        signatures, limited to remaining empty slots.
        """
        remaining_slots = config.images_to_process - len(state.locked_columns)
        if remaining_slots <= 0:
            return []

        candidates = [a for a in attachments if a.signature not in state.last_ids]
        sorted_atts = self.sort_attachments(candidates, newest_first=False)
        return sorted_atts[:remaining_slots]

    def get_target_columns(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
        columns_with_images: Optional[Set[int]] = None,
    ) -> Dict[int, int]:
        """
        Map each attachment (by list position) to the next available column,
        skipping column indices that are already locked AND column IDs that
        currently hold an image in the live row (manually uploaded by the
        user — SH-105).
        """
        filled = columns_with_images or set()
        image_cols = config.image_columns
        available_col_ids = [
            col_id
            for col_idx, col_id in enumerate(image_cols)
            if col_id
            and col_idx not in state.locked_columns
            and col_id not in filled
        ]
        return {
            att_idx: col_id
            for att_idx, col_id in enumerate(available_col_ids[: len(attachments)])
        }

    def update_state_after_processing(
        self,
        state: RowState,
        processed_attachments: List[Attachment],
        target_columns: Dict[int, int],
        config=None,
    ) -> RowState:
        """
        Update state after processing.

        Records processed attachment signatures and locks the correct column
        indices. When config is provided, derives the column index from
        config.image_columns so locking is correct even when processing starts
        from a non-zero slot. Falls back to attachment-index == column-index
        when config is unavailable (legacy behaviour).

        Note: locked_columns stores INDICES (0, 1, 2…) not column IDs.
        """
        signatures = {att.signature for att in processed_attachments}
        state.last_ids.update(signatures)

        if processed_attachments:
            max_ts = max(att.created_timestamp for att in processed_attachments)
            if max_ts > state.last_ts:
                state.last_ts = max_ts

        if config is not None:
            image_cols = list(config.image_columns or [])
            for col_id in target_columns.values():
                try:
                    col_idx = image_cols.index(col_id)
                    state.lock_column(col_idx)
                except ValueError:
                    pass
        else:
            for att_idx in target_columns.keys():
                state.lock_column(att_idx)

        return state
