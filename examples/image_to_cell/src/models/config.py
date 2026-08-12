"""
Configuration Models
====================

Pydantic models for Image-to-Cell configurations.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


class ProcessingMode(str, Enum):
    """Processing mode for configuration."""
    WEBHOOK = "webhook"
    WEBHOOK_V3 = "webhook_v3"  # Same as webhook but v2 ignores it (for isolated testing)
    POLLING = "polling"


class ConfigType(str, Enum):
    """Configuration type determining how images are placed."""
    DEFAULT = "default"
    REPLACE = "replace"
    NAME_MAP = "name map"
    SHIFT = "shift"  # Deprecated


class ImageToCellConfig(BaseModel):
    """
    Image-to-Cell configuration.

    Represents a single configuration that defines how images
    are processed for a specific worksheet.
    """

    id: str = Field(..., description="Unique configuration ID (UUID)")
    organization_id: str = Field(..., description="Organization ID")
    worksheet_id: int = Field(..., description="Smartsheet worksheet ID")
    workspace_name: Optional[str] = Field(None, description="Workspace name (cached)")
    worksheet_name: Optional[str] = Field(None, description="Worksheet name (cached)")
    configuration_name: str = Field(..., description="User-defined configuration name")

    # Processing settings
    processing_mode: ProcessingMode = Field(
        default=ProcessingMode.WEBHOOK,
        description="How changes are detected"
    )
    config_type: ConfigType = Field(
        default=ConfigType.DEFAULT,
        description="How images are placed in cells"
    )
    paused: bool = Field(default=False, description="Whether processing is paused")
    images_to_process: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of images to process per row"
    )

    # Image columns (up to 10)
    image1_column_id: Optional[int] = None
    image1_column_name: Optional[str] = None
    image2_column_id: Optional[int] = None
    image2_column_name: Optional[str] = None
    image3_column_id: Optional[int] = None
    image3_column_name: Optional[str] = None
    image4_column_id: Optional[int] = None
    image4_column_name: Optional[str] = None
    image5_column_id: Optional[int] = None
    image5_column_name: Optional[str] = None
    image6_column_id: Optional[int] = None
    image6_column_name: Optional[str] = None
    image7_column_id: Optional[int] = None
    image7_column_name: Optional[str] = None
    image8_column_id: Optional[int] = None
    image8_column_name: Optional[str] = None
    image9_column_id: Optional[int] = None
    image9_column_name: Optional[str] = None
    image10_column_id: Optional[int] = None
    image10_column_name: Optional[str] = None

    # Name-map mode patterns (SH-112 / SH-220). For each image column slot the
    # user configures a `WHERE filename CONTAINS <pattern>` rule in the UI.
    # Only consulted when `config_type == "name map"`. A blank/null pattern
    # skips that slot — there is no legacy fuzzy match against column names.
    namemap_column1: Optional[str] = None
    namemap_column2: Optional[str] = None
    namemap_column3: Optional[str] = None
    namemap_column4: Optional[str] = None
    namemap_column5: Optional[str] = None
    namemap_column6: Optional[str] = None
    namemap_column7: Optional[str] = None
    namemap_column8: Optional[str] = None
    namemap_column9: Optional[str] = None
    namemap_column10: Optional[str] = None

    # Ownership
    created_by: Optional[str] = Field(None, description="User who created the config")
    shared_with_org: bool = Field(default=False, description="Visible to entire org")

    # SH-178: runner identity. Background work (webhooks, processing,
    # recovery) runs with the runner's Smartsheet token; interactive flows
    # keep using the session user. Starts as created_by and changes only via
    # failover or an admin reassign. connection_status flips to
    # "needs_connection" when the runner's OAuth is gone and no org member
    # qualifies for automatic failover.
    runner_user_id: Optional[str] = Field(
        None,
        description="User whose token executes background work (defaults to created_by)",
    )
    connection_status: str = Field(
        default="active",
        description="'active' or 'needs_connection'",
    )

    # Timestamps
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None

    @property
    def image_columns(self) -> List[int]:
        """Get list of configured image column IDs."""
        columns = []
        for i in range(1, 11):
            col_id = getattr(self, f"image{i}_column_id")
            if col_id:
                columns.append(col_id)
        return columns

    @property
    def image_column_names(self) -> List[str]:
        """Get list of configured image column names."""
        names = []
        for i in range(1, 11):
            col_name = getattr(self, f"image{i}_column_name")
            if col_name:
                names.append(col_name)
        return names

    def get_column_name(self, column_id: int) -> Optional[str]:
        """Get the name for a column ID."""
        for i in range(1, 11):
            if getattr(self, f"image{i}_column_id") == column_id:
                return getattr(self, f"image{i}_column_name")
        return None

    @property
    def is_active(self) -> bool:
        """Check if configuration is active (not paused)."""
        return not self.paused

    @property
    def runner(self) -> Optional[str]:
        """SH-178: identity for background work — runner with created_by
        fallback (rows written before the runner_user_id backfill, or by
        older code during the rollout window)."""
        return self.runner_user_id or self.created_by

    @property
    def needs_connection(self) -> bool:
        """SH-178: True when the config lost its runner and no failover
        candidate qualified — background work is on hold until a user
        reconnects or an admin reassigns the runner."""
        return self.connection_status == "needs_connection"

    class Config:
        from_attributes = True


class ConfigCreate(BaseModel):
    """Model for creating a new configuration."""

    organization_id: str
    worksheet_id: int
    configuration_name: str
    processing_mode: ProcessingMode = ProcessingMode.WEBHOOK
    config_type: ConfigType = ConfigType.DEFAULT
    images_to_process: int = Field(default=1, ge=1, le=10)
    image_columns: List[int] = Field(..., min_length=1, max_length=10)
    created_by: str


class ConfigUpdate(BaseModel):
    """Model for updating a configuration."""

    configuration_name: Optional[str] = None
    paused: Optional[bool] = None
    processing_mode: Optional[ProcessingMode] = None
    config_type: Optional[ConfigType] = None
    images_to_process: Optional[int] = Field(None, ge=1, le=10)
