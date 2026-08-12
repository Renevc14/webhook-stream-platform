"""
Name Map Processing Mode
========================

Map attachment filenames to specific columns.
"""

import logging
from typing import List, Dict, Optional, Set

from src.models.config import ImageToCellConfig
from src.models.attachment import Attachment
from src.models.row_state import RowState
from src.core.modes.base import BaseMode


log = logging.getLogger(__name__)


class NameMapMode(BaseMode):
    """
    Name map processing mode.

    Behavior:
    - Matches attachment filenames to column names
    - Uses pattern matching (contains, prefix, suffix)
    - Only processes attachments that match a column
    - Tracks per-attachment failures and stops retrying after a bounded budget
    """

    def __init__(self, max_upload_attempts: int = 5):
        self._max_upload_attempts = max_upload_attempts

    @property
    def name(self) -> str:
        return "name map"

    def get_pending_attachments(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> List[Attachment]:
        """
        Return attachments that still need an upload attempt.

        An attachment is pending when:
        - It matches a configured column
        - Its signature is not already in last_ids (successful upload)
        - It has not exhausted the retry budget in failed_attempts
        """
        pending: List[Attachment] = []
        for att in attachments:
            sig = att.signature
            if state.is_attachment_processed(sig):
                continue
            if state.is_attachment_terminal_failure(sig, self._max_upload_attempts):
                log.debug(
                    "[name_map] row=%s att=%r skip — exhausted %d upload attempts",
                    state.row_id,
                    att.name,
                    self._max_upload_attempts,
                )
                continue
            if self._find_matching_column(att, config) is None:
                continue
            pending.append(att)
        return pending

    def should_process_row(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> bool:
        """
        Check if a row should be processed.

        Row should be processed if at least one attachment is still pending.
        """
        if not attachments:
            log.info(
                "[name_map.should_process_row] row=%s skip — no processable attachments",
                state.row_id,
            )
            return False

        pending = self.get_pending_attachments(attachments, state, config)
        if not pending:
            log.info(
                "[name_map.should_process_row] row=%s skip — no pending attachments "
                "(processed=%d, failed_tracked=%d)",
                state.row_id,
                len(state.last_ids),
                len(state.failed_attempts),
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
        Filter to attachments that still need processing for name_map mode.
        """
        return self.get_pending_attachments(attachments, state, config)

    def get_target_columns(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
        columns_with_images: Optional[Set[int]] = None,
    ) -> Dict[int, int]:
        """
        Map attachments to columns by name matching.

        Each attachment is mapped to the column whose name
        best matches the attachment filename.

        Filename-based mapping is intrinsic to name_map, so an existing
        image in the target column does not change the routing decision;
        columns_with_images is accepted for signature parity but ignored.
        """
        mapping = {}

        for idx, att in enumerate(attachments):
            col_id = self._find_matching_column(att, config)
            if col_id is not None:
                mapping[idx] = col_id

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

        Records processed attachments. In name map mode,
        the same column can receive different images over time.
        """
        # Add processed attachment signatures
        signatures = {att.signature for att in processed_attachments}
        state.last_ids.update(signatures)

        # Update timestamp to newest processed
        if processed_attachments:
            max_ts = max(att.created_timestamp for att in processed_attachments)
            if max_ts > state.last_ts:
                state.last_ts = max_ts

        return state

    def record_processing_outcomes(
        self,
        state: RowState,
        attachments: List[Attachment],
        results: List,
    ) -> RowState:
        """
        Track per-attachment upload failures for bounded retries.

        Successful uploads are recorded via update_state_after_processing;
        this method only maintains failed_attempts for failures.
        """
        for att, result in zip(attachments, results):
            if getattr(result, "success", False):
                state.clear_failed_attempt(att.signature)
            else:
                state.record_failed_attempt(att.signature)
        return state

    def _find_matching_column(
        self,
        attachment: Attachment,
        config: ImageToCellConfig,
    ) -> Optional[int]:
        """
        Find the column that matches an attachment filename.

        SH-112 / SH-220: only the user-defined CONTAINS pattern
        (``namemap_column{i}`` — "WHERE filename CONTAINS …") is used.
        First matching slot wins. A blank/null pattern skips that slot;
        there is **no** legacy fuzzy match against ``image{i}_column_name``
        (that silent fallback caused partial row matches for customers).

        Returns:
            Column ID or None if no match
        """
        filename = attachment.name.lower()

        for i in range(1, 11):
            col_id = getattr(config, f"image{i}_column_id", None)
            if not col_id:
                continue
            pattern = getattr(config, f"namemap_column{i}", None)
            if not pattern:
                continue
            if pattern.lower() in filename:
                log.info(
                    "[SH-112:name_map] attachment=%r matched col=%s by CONTAINS pattern=%r",
                    attachment.name, col_id, pattern,
                )
                return col_id

        return None
