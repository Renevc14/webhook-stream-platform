"""
Replace Processing Mode
=======================

Always replace with the newest images.
"""

import logging
from typing import List, Dict, Optional, Set

from src.models.config import ImageToCellConfig
from src.models.attachment import Attachment
from src.models.row_state import RowState
from src.core.modes.base import BaseMode


log = logging.getLogger(__name__)


class ReplaceMode(BaseMode):
    """
    Replace processing mode.

    Behavior:
    - Takes the newest N images
    - Clears existing images in target columns
    - Places new images
    - Processes again when new attachments are added
    """

    @property
    def name(self) -> str:
        return "replace"

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
        - There are new attachments since last processing
        """
        if not attachments:
            log.info(
                "[replace.should_process_row] row=%s skip — no processable attachments",
                state.row_id,
            )
            return False

        # Check for new attachments
        new_attachments = self.get_new_attachments(attachments, state)
        if not new_attachments:
            log.info(
                "[replace.should_process_row] row=%s skip — no new attachments since last_ts=%s (have %d, %d processed)",
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
        Get the newest N attachments to place into the N target columns.

        SH-111: returns ALL the newest N every time, including signatures
        already in `state.last_ids`. Replace mode's invariant is
        "newest N visible in N columns" — when a new attachment arrives
        the older ones must be re-placed in higher-indexed columns to
        preserve the order, not skipped just because they were processed
        before. Tier T1 (`row_processor._apply_existing_image_tiers`)
        catches no-op cases where the target cell already has the right
        image; row_processor.process exempts shuffled attachments
        (already in last_ids before this run) from usage_log so they
        do not double-bill the customer.

        Earlier behaviour: this function dropped attachments whose
        signature was in `last_ids`. That prevented re-billing but also
        prevented the "shuffle on new arrival" needed to fill columns
        1..N-1 after a sequence of single-attachment webhooks.
        """
        n_slots = config.images_to_process
        sorted_atts = self.sort_attachments(attachments, newest_first=True)
        return sorted_atts[:n_slots]

    def get_target_columns(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
        columns_with_images: Optional[Set[int]] = None,
    ) -> Dict[int, int]:
        """
        Map attachments to columns by index.

        Attachment 0 (newest) -> Column 0
        Attachment 1 -> Column 1
        etc.

        Replace always targets the same positional columns regardless of
        whether they already hold an image; columns_with_images is accepted
        to satisfy the base signature but intentionally ignored.
        """
        columns = config.image_columns
        mapping = {}

        for idx, att in enumerate(attachments):
            if idx < len(columns):
                mapping[idx] = columns[idx]

        return mapping

    def update_state_after_processing(
        self,
        state: RowState,
        processed_attachments: List[Attachment],
        target_columns: Dict[int, int],
        config=None,
    ) -> RowState:
        """
        Update state after processing.

        In replace mode, we update the timestamp but don't "lock"
        columns since they can be replaced again.
        """
        # Add processed attachment signatures
        signatures = {att.signature for att in processed_attachments}
        state.last_ids.update(signatures)

        # Update timestamp to newest processed
        if processed_attachments:
            max_ts = max(att.created_timestamp for att in processed_attachments)
            if max_ts > state.last_ts:
                state.last_ts = max_ts

        # Note: In replace mode, we don't lock columns
        # They can be replaced on the next run

        return state

    def should_clear_cells(self) -> bool:
        """
        Indicate that cells should be cleared before upload.

        Replace mode clears existing images first.
        """
        return True
