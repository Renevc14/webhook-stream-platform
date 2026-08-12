"""
Result Models
=============

Pydantic models for processing results.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ImageResult(BaseModel):
    """Result of processing a single image."""

    success: bool = Field(..., description="Whether processing succeeded")
    attachment_id: int = Field(..., description="Attachment ID")
    filename: str = Field(..., description="Original filename")
    column_id: Optional[int] = Field(None, description="Target column ID")

    # Metrics
    original_size_bytes: int = Field(default=0)
    processed_size_bytes: int = Field(default=0)
    processing_time_ms: int = Field(default=0)

    # Error info (if failed)
    error: Optional[str] = None
    error_type: Optional[str] = None

    @property
    def compression_ratio(self) -> float:
        """Calculate compression ratio."""
        if self.original_size_bytes == 0:
            return 0.0
        return self.processed_size_bytes / self.original_size_bytes


class RowResult(BaseModel):
    """Result of processing a single row."""

    success: bool = Field(..., description="Whether any images were processed")
    row_id: int = Field(..., description="Row ID")

    # Image results
    images_processed: int = Field(default=0)
    images_skipped: int = Field(default=0)
    images_failed: int = Field(default=0)
    image_results: List[ImageResult] = Field(default_factory=list)

    # Timing
    processing_time_ms: int = Field(default=0)

    # Error info (if completely failed)
    error: Optional[str] = None

    @property
    def total_images(self) -> int:
        """Get total images attempted."""
        return self.images_processed + self.images_skipped + self.images_failed


class ProcessingResult(BaseModel):
    """Result of processing a configuration."""

    success: bool = Field(..., description="Whether processing succeeded")
    config_id: str = Field(..., description="Configuration ID")
    worksheet_id: int = Field(..., description="Worksheet ID")

    # Row results
    rows_processed: int = Field(default=0)
    rows_skipped: int = Field(default=0)
    rows_failed: int = Field(default=0)
    row_results: List[RowResult] = Field(default_factory=list)

    # Image totals
    total_images_processed: int = Field(default=0)
    total_images_skipped: int = Field(default=0)
    total_images_failed: int = Field(default=0)

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    processing_time_ms: int = Field(default=0)

    # Error info
    error: Optional[str] = None

    @property
    def total_rows(self) -> int:
        """Get total rows attempted."""
        return self.rows_processed + self.rows_skipped + self.rows_failed

    def add_row_result(self, result: RowResult) -> None:
        """Add a row result and update totals."""
        self.row_results.append(result)

        if result.success:
            self.rows_processed += 1
        elif result.error:
            self.rows_failed += 1
        else:
            self.rows_skipped += 1

        self.total_images_processed += result.images_processed
        self.total_images_skipped += result.images_skipped
        self.total_images_failed += result.images_failed


class BackfillResult(BaseModel):
    """Result of backfill operation."""

    success: bool = Field(default=True)
    worksheet_id: int = Field(..., description="Worksheet ID")

    # Processing stats
    rows_processed: int = Field(default=0)
    rows_skipped: int = Field(default=0)
    rows_failed: int = Field(default=0)

    # Sheet fetch
    sheet_fetched: bool = Field(default=False)
    total_rows_in_sheet: int = Field(default=0)
    rows_with_attachments: int = Field(default=0)

    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    processing_time_ms: int = Field(default=0)

    # Error info
    error: Optional[str] = None

    def merge(self, other: "BackfillResult") -> None:
        """Merge another backfill result into this one."""
        self.rows_processed += other.rows_processed
        self.rows_skipped += other.rows_skipped
        self.rows_failed += other.rows_failed

        if other.error and not self.error:
            self.error = other.error
            self.success = False


class WebhookResult(BaseModel):
    """Result of webhook event processing."""

    success: bool = Field(default=True)
    webhook_id: Optional[int] = None
    worksheet_id: Optional[int] = None

    # Event processing
    events_received: int = Field(default=0)
    events_processed: int = Field(default=0)
    events_skipped: int = Field(default=0)
    events_failed: int = Field(default=0)

    # Attachments
    attachments_processed: int = Field(default=0)

    # Status
    status: str = Field(default="ok")
    message: Optional[str] = None

    # Timing
    processing_time_ms: int = Field(default=0)


class HealthCheckResult(BaseModel):
    """Result of health check."""

    status: str = Field(default="ok")
    version: str = Field(default="3.0.0")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Component status
    database: bool = Field(default=True)
    smartsheet: bool = Field(default=True)

    # Stats
    uptime_seconds: Optional[int] = None
    active_webhooks: Optional[int] = None
    configs_count: Optional[int] = None

    # Error details
    errors: List[str] = Field(default_factory=list)
