"""
Row State Models
================

Pydantic models for tracking row processing state.
"""

from datetime import datetime
from typing import Optional, Set, List, Dict
from pydantic import BaseModel, Field


class RowState(BaseModel):
    """
    Row processing state.

    Tracks which attachments have been processed for a row
    to enable deduplication and prevent reprocessing.
    """

    config_id: str = Field(..., description="Configuration ID")
    row_id: int = Field(..., description="Smartsheet row ID")

    # Processing state
    last_ts: float = Field(
        default=0.0,
        description="Timestamp of last processed attachment"
    )
    last_ids: Set[str] = Field(
        default_factory=set,
        description="Set of processed attachment signatures"
    )

    # Column locking (for default mode)
    locked_columns: List[int] = Field(
        default_factory=list,
        description="Column IDs that have been filled"
    )

    # Per-attachment failure counts (name_map mode only; unused by default/replace)
    failed_attempts: Dict[str, int] = Field(
        default_factory=dict,
        description="Upload attempt counts keyed by attachment signature",
    )

    # Timestamps
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def processed_count(self) -> int:
        """Get count of processed attachments."""
        return len(self.last_ids)

    def is_attachment_processed(self, attachment_signature: str) -> bool:
        """Check if an attachment has been processed."""
        return attachment_signature in self.last_ids

    def get_failed_attempt_count(self, attachment_signature: str) -> int:
        """Return recorded failed upload attempts for an attachment signature."""
        return int(self.failed_attempts.get(attachment_signature, 0))

    def is_attachment_terminal_failure(
        self,
        attachment_signature: str,
        max_attempts: int,
    ) -> bool:
        """True when an attachment exhausted its retry budget without success."""
        return self.get_failed_attempt_count(attachment_signature) >= max_attempts

    def record_failed_attempt(self, attachment_signature: str) -> None:
        """Increment the failed upload counter for an attachment signature."""
        self.failed_attempts[attachment_signature] = (
            self.get_failed_attempt_count(attachment_signature) + 1
        )

    def clear_failed_attempt(self, attachment_signature: str) -> None:
        """Remove failure tracking after a successful upload."""
        self.failed_attempts.pop(attachment_signature, None)

    def is_attachment_newer(self, attachment_timestamp: float) -> bool:
        """Check if an attachment is newer than last processed."""
        return attachment_timestamp > self.last_ts

    def should_process_attachment(
        self,
        signature: str,
        timestamp: float,
    ) -> bool:
        """
        Check if an attachment should be processed.

        Args:
            signature: Attachment signature
            timestamp: Attachment creation timestamp

        Returns:
            True if attachment should be processed
        """
        if self.is_attachment_processed(signature):
            return False
        return self.is_attachment_newer(timestamp)

    def mark_processed(
        self,
        signatures: Set[str],
        max_timestamp: float,
    ) -> None:
        """
        Mark attachments as processed.

        Args:
            signatures: Set of processed attachment signatures
            max_timestamp: Maximum timestamp among processed attachments
        """
        self.last_ids.update(signatures)
        if max_timestamp > self.last_ts:
            self.last_ts = max_timestamp
        self.updated_at = datetime.utcnow()

    def lock_column(self, column_id: int) -> None:
        """Mark a column as filled/locked."""
        if column_id not in self.locked_columns:
            self.locked_columns.append(column_id)

    def is_column_locked(self, column_id: int) -> bool:
        """Check if a column is locked."""
        return column_id in self.locked_columns

    def available_slots(self, total_slots: int) -> int:
        """Get number of available slots for new images."""
        return max(0, total_slots - len(self.locked_columns))

    class Config:
        from_attributes = True


class RowStateCreate(BaseModel):
    """Model for creating a new row state."""

    config_id: str
    row_id: int
    last_ts: float = 0.0
    last_ids: Set[str] = Field(default_factory=set)


class RowStateUpdate(BaseModel):
    """Model for updating row state."""

    last_ts: Optional[float] = None
    last_ids: Optional[Set[str]] = None
    locked_columns: Optional[List[int]] = None
    failed_attempts: Optional[Dict[str, int]] = None
