"""
Base Processing Mode
====================

Abstract base class for processing modes.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Set

from src.models.config import ImageToCellConfig
from src.models.attachment import Attachment
from src.models.row_state import RowState
from src.models.results import RowResult


class BaseMode(ABC):
    """
    Abstract base class for image processing modes.

    Each mode determines how images are placed into cells:
    - Default: Fill N slots once, then lock
    - Replace: Always replace with newest images
    - NameMap: Match filenames to specific columns
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the mode name."""
        pass

    @abstractmethod
    def filter_attachments(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> List[Attachment]:
        """
        Filter attachments to determine which should be processed.

        Args:
            attachments: All attachments on the row
            state: Current row processing state
            config: Configuration

        Returns:
            List of attachments to process
        """
        pass

    @abstractmethod
    def get_target_columns(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
        columns_with_images: Optional[Set[int]] = None,
    ) -> Dict[int, int]:
        """
        Map attachments to target columns.

        Args:
            attachments: Attachments to place
            state: Current row state
            config: Configuration
            columns_with_images: Column IDs that already hold an image in
                the live row (default mode uses this to find the next empty
                slot — see SH-105). Other modes accept and ignore.

        Returns:
            Dict mapping attachment index to column ID
        """
        pass

    @abstractmethod
    def should_process_row(
        self,
        attachments: List[Attachment],
        state: RowState,
        config: ImageToCellConfig,
    ) -> bool:
        """
        Check if a row should be processed.

        Args:
            attachments: Row attachments
            state: Current row state
            config: Configuration

        Returns:
            True if row should be processed
        """
        pass

    @abstractmethod
    def update_state_after_processing(
        self,
        state: RowState,
        processed_attachments: List[Attachment],
        target_columns: Dict[int, int],
        config=None,
    ) -> RowState:
        """
        Update row state after processing.

        Args:
            state: Current state
            processed_attachments: Attachments that were processed
            target_columns: Columns that received images

        Returns:
            Updated state
        """
        pass

    def sort_attachments(
        self,
        attachments: List[Attachment],
        newest_first: bool = False,
    ) -> List[Attachment]:
        """
        Sort attachments by creation time.

        Args:
            attachments: Attachments to sort
            newest_first: If True, newest first; otherwise oldest first

        Returns:
            Sorted list of attachments
        """
        return sorted(
            attachments,
            key=lambda a: a.created_timestamp,
            reverse=newest_first,
        )

    def get_new_attachments(
        self,
        attachments: List[Attachment],
        state: RowState,
    ) -> List[Attachment]:
        """
        Get attachments that are newer than the last processed.

        Args:
            attachments: All attachments
            state: Current state

        Returns:
            List of new attachments
        """
        return [
            att for att in attachments
            if att.created_timestamp > state.last_ts
        ]

    def get_unprocessed_attachments(
        self,
        attachments: List[Attachment],
        state: RowState,
    ) -> List[Attachment]:
        """
        Get attachments that haven't been processed yet.

        Args:
            attachments: All attachments
            state: Current state

        Returns:
            List of unprocessed attachments
        """
        return [
            att for att in attachments
            if att.signature not in state.last_ids
        ]
