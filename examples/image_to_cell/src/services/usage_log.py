"""
Usage Log Service
=================

Logs image-to-cell usage to imagetocell_usage_log for billing and overage tracking.
Uses the same log_image_conversion RPC as image-to-cell_2.
"""

import logging
from typing import Optional

from src.services.supabase.client import get_supabase_service

log = logging.getLogger(__name__)

# Fallback user_id when config.created_by is None (e.g. system-created configs)
# Must be a valid UUID; org's plan owner would be ideal but requires extra query.
SYSTEM_USER_FALLBACK = "00000000-0000-0000-0000-000000000001"


# Postgres SQLSTATE for unique_violation. Historically surfaced when the
# SH-98 partial UNIQUE INDEX imagetocell_usage_log_unique_per_processing_idx
# rejected a duplicate success row (migration 20260515000000). That index
# is dropped by 20260803190000 so reprocess can insert an additional
# billable row; this classification remains for residual indexes / races
# until staging applies the migration.
_UNIQUE_VIOLATION_SQLSTATE = "23505"

# Sentinel returned when a unique index still rejects a duplicate successful
# conversion. Distinct from ``None`` (transient RPC / unexpected failure).
# Callers that completed a real image write must NOT refund Redis solely
# because of this sentinel — product bills every processed image; Hub needs
# a durable usage_log row (see migration 20260803190000).
ALREADY_LOGGED = "already_logged"


def _is_unique_violation(exc: BaseException) -> bool:
    """``True`` if ``exc`` is a Postgres unique_violation on usage_log.

    Checks both the ``.code`` attribute (set by postgrest-py's APIError
    for PostgREST errors) and a substring fallback on the message so the
    classification holds even if the exception type changes between
    supabase-py versions.
    """
    code = getattr(exc, "code", None)
    if code == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    msg = str(exc).lower()
    return (
        _UNIQUE_VIOLATION_SQLSTATE in msg
        or "duplicate key value violates unique constraint" in msg
        or "imagetocell_usage_log_unique_per_processing_idx" in msg.lower()
        or "imagetocell_usage_log_unique_conversion" in msg.lower()
    )


def log_image_conversion(
    organization_id: str,
    user_id: Optional[str],
    config_id: str,
    config_name: str,
    config_type: str,
    worksheet_id: int,
    worksheet_name: Optional[str],
    row_id: int,
    image_column_id: int,
    image_column_name: Optional[str],
    image_filename: str,
    success: bool,
    image_size_bytes: Optional[int] = None,
    processing_duration_ms: Optional[int] = None,
    error_message: Optional[str] = None,
    is_system_generated: bool = False,
) -> Optional[str]:
    """
    Log a single image conversion to imagetocell_usage_log.

    The BEFORE INSERT trigger sets is_overage based on check_org_overage_state.
    Used for billing, overage tracking, and get_i2c_overage_breakdown.

    Returns:
        UUID string of the created usage log entry on insert;
        ``ALREADY_LOGGED`` when a residual UNIQUE INDEX rejects a
        duplicate; ``None`` on any other failure (transient RPC error,
        empty RPC payload, etc.).

        After migration 20260803190000, successful reprocess inserts a
        new row (Hub counts every success). ``ALREADY_LOGGED`` should be
        rare; callers that already wrote the image must keep the bolt
        charge rather than refunding on this sentinel alone.
    """
    uid = user_id or SYSTEM_USER_FALLBACK
    try:
        db = get_supabase_service()
        result = db.client.rpc(
            "log_image_conversion",
            {
                "p_organization_id": organization_id,
                "p_user_id": uid,
                "p_config_id": config_id,
                "p_config_name": config_name,
                "p_config_type": config_type,
                "p_worksheet_id": worksheet_id,
                "p_worksheet_name": worksheet_name or "",
                "p_row_id": row_id,
                "p_image_column_id": image_column_id,
                "p_image_column_name": image_column_name or "",
                "p_image_filename": image_filename,
                "p_image_size_bytes": image_size_bytes,
                "p_processing_duration_ms": processing_duration_ms,
                "p_success": success,
                "p_error_message": error_message,
                "p_is_system_generated": is_system_generated,
            },
        ).execute()

        if result.data:
            log.debug(f"Logged image conversion: {image_filename} success={success}")
            return str(result.data)
        return None
    except Exception as e:
        # Residual unique index (pre-migration) or rare race. Product bills
        # successful work; RowProcessor keeps the Redis hold and warns.
        if _is_unique_violation(e):
            log.info(
                "[USAGE_LOG] Duplicate prevented at DB level — "
                "org=%s sheet=%s row=%s col=%s file=%s. "
                "Returning ALREADY_LOGGED (do not refund if image was written; "
                "apply migration 20260803190000 for durable reprocess rows).",
                organization_id,
                worksheet_id,
                row_id,
                image_column_id,
                image_filename,
            )
            return ALREADY_LOGGED

        from src.utils.safe_log import safe_exception_message
        log.error(f"Failed to log image conversion: {safe_exception_message(e)}")
        return None
