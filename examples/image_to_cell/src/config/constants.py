"""
Constants Module
================

All magic numbers and constant values in one place.
These values should NOT be configurable via environment variables.
"""

from typing import Set, Dict, FrozenSet


# =============================================================================
# Product Identity
# =============================================================================

PRODUCT_SLUG = "smart-image-to-cell"
PRODUCT_VERSION = "3.0.0"
CHANGE_AGENT_ID = "smart-image-to-cell-v3.0"


# =============================================================================
# Database Table Names
# =============================================================================

class TABLE_NAMES:
    """Supabase table names."""

    CONFIG = "imagetocell_config"
    ROW_STATES = "imagetocell_row_states"
    WEBHOOKS = "imagetocell_webhooks"
    WEBHOOKS_PENDING = "imagetocell_webhooks_pending"
    USAGE_LOG = "imagetocell_usage_log"
    DAILY_USAGE = "imagetocell_daily_usage"
    MONTHLY_USAGE = "imagetocell_monthly_usage"
    YEARLY_USAGE = "imagetocell_yearly_usage"
    MANUAL_TRIGGERS = "imagetocell_manual_triggers"
    FAILED_EVENTS = "imagetocell_failed_events"

    # External tables
    TOKENS = "smartsheet_tokens"
    ORG_BASE = "organization_ss_api_base"

    # Views
    VIEW_CONFIG_USAGE = "v_config_usage_summary"
    VIEW_ORG_USAGE = "v_organization_usage_summary"
    VIEW_USER_USAGE = "v_user_usage_summary"


# =============================================================================
# Image Processing Limits
# =============================================================================

class IMAGE_LIMITS:
    """Image processing constraints."""

    # Dimensions
    MIN_DIMENSION_PX = 50
    MAX_DIMENSION_PX = 10000
    TARGET_MAX_PX = 720

    # File sizes
    MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024  # 30MB
    MIN_FILE_SIZE_BYTES = 100  # 100 bytes

    # Processing
    MAX_IMAGES_PER_ROW = 10
    MIN_ATTACHMENT_AGE_S = 1  # Avoid race conditions

    # JPEG quality levels (will try in order until size is acceptable)
    JPEG_QUALITIES = (75, 65, 50, 40)

    # PDF processing
    PDF_DPI = 150
    PDF_MAX_PAGES = 1  # Only first page


# =============================================================================
# Supported File Formats
# =============================================================================

class SUPPORTED_FORMATS:
    """Supported file formats for processing."""

    # Image formats
    IMAGES: FrozenSet[str] = frozenset({
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/bmp",
        "image/tiff",
    })

    # Document formats that can be converted to images
    DOCUMENTS: FrozenSet[str] = frozenset({
        "application/pdf",
    })

    # All supported formats
    ALL: FrozenSet[str] = IMAGES | DOCUMENTS

    # File extensions mapping
    EXTENSIONS: Dict[str, str] = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".pdf": "application/pdf",
    }

    # SVG (beta, requires special handling)
    SVG_MIME = "image/svg+xml"


# =============================================================================
# Processing Modes
# =============================================================================

class PROCESSING_MODES:
    """Available processing modes."""

    WEBHOOK = "webhook"
    POLLING = "polling"


class CONFIG_TYPES:
    """Available configuration types."""

    DEFAULT = "default"
    REPLACE = "replace"
    NAME_MAP = "name map"
    SHIFT = "shift"  # Deprecated


# =============================================================================
# HTTP Headers
# =============================================================================

class HTTP_HEADERS:
    """Standard HTTP headers used in the application."""

    USER_ID = "X-User-Id"
    CHANGE_AGENT = "X-Smartsheet-Change-Agent"
    WEBHOOK_SIGNATURE = "X-Smartsheet-Hmac-Sha256"
    WEBHOOK_CHALLENGE = "Smartsheet-Hook-Challenge"
    WEBHOOK_RESPONSE = "Smartsheet-Hook-Response"
    CONTENT_TYPE = "Content-Type"
    AUTHORIZATION = "Authorization"


# =============================================================================
# Webhook Constants
# =============================================================================

class WEBHOOK_CONSTANTS:
    """Webhook-related constants."""

    # Event types
    EVENT_CREATED = "created"
    EVENT_UPDATED = "updated"
    EVENT_DELETED = "deleted"

    # Object types
    OBJECT_ATTACHMENT = "attachment"
    OBJECT_ROW = "row"
    OBJECT_CELL = "cell"

    # Status
    STATUS_ENABLED = "ENABLED"
    STATUS_DISABLED = "DISABLED"
    STATUS_NEW_NOT_VERIFIED = "NEW_NOT_VERIFIED"

    # Scope
    SCOPE_SHEET = "sheet"

    # Callback version
    CALLBACK_VERSION = 1


# =============================================================================
# Error Messages
# =============================================================================

class ERROR_MESSAGES:
    """Standard error messages."""

    NO_USER_ID = "Missing X-User-Id header"
    INVALID_CONFIG = "Invalid configuration ID"
    NO_TOKEN = "No Smartsheet token found for organization"
    RATE_LIMITED = "Too many requests"
    WEBHOOK_SIGNATURE_INVALID = "Invalid webhook signature"
    WEBHOOK_SECRET_MISSING = "Webhook secret not found"
    IMAGE_TOO_SMALL = "Image dimensions too small"
    IMAGE_TOO_LARGE = "Image dimensions too large"
    UNSUPPORTED_FORMAT = "Unsupported file format"


# =============================================================================
# Retry Configuration
# =============================================================================

class RETRY_CONFIG:
    """Default retry configuration."""

    # Retryable HTTP status codes
    RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})

    # Retryable error messages (partial matches)
    RETRYABLE_ERRORS = frozenset({
        "server disconnected",
        "connection reset",
        "connection aborted",
        "remote disconnected",
        "timeout",
        "ssl error",
        "connection refused",
        "broken pipe",
        "connection timed out",
        "read timed out",
        # Windows-specific TCP errors (WinError 10053, 10054)
        "established connection was aborted",
        "winerror 10053",
        "winerror 10054",
        "software caused connection abort",
        "an existing connection was forcibly closed",
    })

    # HTTP methods that can be retried
    RETRYABLE_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})


# =============================================================================
# Cache Keys
# =============================================================================

class CACHE_KEYS:
    """Cache key prefixes."""

    TOKEN = "token:"
    SHEET = "sheet:"
    CONFIG = "config:"
    ROW_STATE = "row_state:"
    WEBHOOK = "webhook:"
