"""Configuration module - centralized settings and constants."""

from .settings import Settings, get_settings
from .constants import (
    PRODUCT_SLUG,
    CHANGE_AGENT_ID,
    TABLE_NAMES,
    IMAGE_LIMITS,
    SUPPORTED_FORMATS,
)

__all__ = [
    "Settings",
    "get_settings",
    "PRODUCT_SLUG",
    "CHANGE_AGENT_ID",
    "TABLE_NAMES",
    "IMAGE_LIMITS",
    "SUPPORTED_FORMATS",
]
