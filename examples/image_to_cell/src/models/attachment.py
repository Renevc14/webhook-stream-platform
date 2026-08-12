"""
Attachment Models
=================

Pydantic models for Smartsheet attachments.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field, field_validator
from dateutil import parser as dtparse


def safe_parse_timestamp(v: Any) -> Optional[datetime]:
    """
    Safely parse a timestamp from various formats.

    Handles Smartsheet's quirky timestamp formats like:
    - '2026-02-07T08:43:43+00:00Z' (extra Z)
    - '2026-02-07T08:43:43Z'
    - '2026-02-07T08:43:43+00:00'
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        # Strip trailing Z that might be extra
        s = str(v).rstrip("Z")
        return dtparse.isoparse(s)
    except Exception:
        return None


class AttachmentType(str, Enum):
    """Type of attachment."""
    FILE = "FILE"
    LINK = "LINK"
    BOX_COM = "BOX_COM"
    DROPBOX = "DROPBOX"
    EGNYTE = "EGNYTE"
    EVERNOTE = "EVERNOTE"
    GOOGLE_DRIVE = "GOOGLE_DRIVE"
    ONEDRIVE = "ONEDRIVE"


class Attachment(BaseModel):
    """
    Smartsheet attachment model.

    Represents an attachment on a row that can be processed
    as an image.
    """

    id: int = Field(..., description="Attachment ID")
    name: str = Field(..., description="Attachment filename")
    mime_type: Optional[str] = Field(None, alias="mimeType", description="MIME type")
    attachment_type: AttachmentType = Field(
        default=AttachmentType.FILE,
        alias="attachmentType",
        description="Type of attachment"
    )

    # URLs
    url: Optional[str] = Field(None, description="Direct URL (if available)")
    download_url: Optional[str] = Field(None, alias="downloadUrl", description="Download URL")

    # Metadata
    size_in_kb: Optional[float] = Field(None, alias="sizeInKb", description="Size in KB")
    created_at: Optional[datetime] = Field(None, alias="createdAt", description="Creation time")
    created_by: Optional[dict] = Field(None, alias="createdBy", description="Creator info")

    # Parent info
    parent_id: Optional[int] = Field(None, alias="parentId", description="Parent row ID")
    parent_type: Optional[str] = Field(None, alias="parentType", description="Parent type (ROW)")

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, v: Any) -> Optional[datetime]:
        """Parse timestamp handling Smartsheet's quirky formats."""
        return safe_parse_timestamp(v)

    @property
    def effective_url(self) -> Optional[str]:
        """Get the URL to use for downloading."""
        return self.download_url or self.url

    @property
    def is_image(self) -> bool:
        """Check if attachment is an image based on MIME type or extension."""
        mime = (self.mime_type or "").lower()

        # Check MIME type (handles both "image/jpeg" and "image" from Smartsheet)
        if mime.startswith("image/") or mime == "image":
            return True

        # Fallback: check file extension
        if self.name:
            ext = self.name.lower().rsplit(".", 1)[-1] if "." in self.name else ""
            image_extensions = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "heif", "tiff", "tif"}
            if ext in image_extensions:
                return True

        return False

    @property
    def is_pdf(self) -> bool:
        """Check if attachment is a PDF."""
        if not self.mime_type:
            return False
        return "pdf" in self.mime_type.lower()

    @property
    def is_processable(self) -> bool:
        """Check if attachment can be processed as an image."""
        return self.is_image or self.is_pdf

    @property
    def created_timestamp(self) -> float:
        """Get creation time as Unix timestamp."""
        if self.created_at:
            return self.created_at.timestamp()
        return 0.0

    @property
    def signature(self) -> str:
        """Get unique signature for deduplication."""
        return f"{self.id}:{self.name}:{self.created_timestamp}"

    class Config:
        from_attributes = True
        populate_by_name = True


class AttachmentDownload(BaseModel):
    """Model for downloaded attachment data."""

    attachment: Attachment
    data: bytes
    size_bytes: int

    @property
    def filename(self) -> str:
        """Get the filename."""
        return self.attachment.name

    @property
    def mime_type(self) -> Optional[str]:
        """Get the MIME type."""
        return self.attachment.mime_type
