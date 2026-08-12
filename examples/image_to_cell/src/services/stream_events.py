"""SH-155 — publish structured outcomes to Redis Streams.

Two additive, best-effort publishers for the platform bus:

- ``publish_image_processed`` → ``stream:platform-events`` after every
  successfully uploaded image. Accumulates for Phase 5 / cross-tool
  workflows; no consumer exists yet.
- ``publish_dlq_abandoned`` → ``stream:i2c:dlq`` when a row failure becomes
  PERMANENT (the Supabase DLQ entry is abandoned: retries exhausted or a
  non-retryable error). The Supabase ``imagetocell_dlq`` table remains the
  primary DLQ — the stream is observability/workflow metadata on top.

Both are strictly best-effort: no Redis client wired (unit tests, degraded
startup) or a failed XADD never breaks the processing path. Every publish
failure is logged on the error path per the ticket.

Envelope follows TARGET_ARCHITECTURE §Stream payload envelope (versioned,
string-valued fields — Redis Streams only accept strings). ``v`` matches
``smarthub_core.worker.base.ENVELOPE_VERSION`` so the future SH-152 consumer
can parse these entries with the same code path.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

TOOL_SLUG = "image-to-cell"
ENVELOPE_VERSION = "1"
PLATFORM_EVENTS_STREAM = "stream:platform-events"
I2C_DLQ_STREAM = "stream:i2c:dlq"


def _get_client():
    # Late import: the client is installed by app lifespan after startup;
    # importing at call time also lets unit tests swap it cleanly.
    from src.services.redis_client import get_redis_client

    return get_redis_client()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _publish(stream: str, envelope: dict) -> Optional[str]:
    client = _get_client()
    if client is None:
        log.debug(
            "[STREAM:EVENTS] no Redis client wired — skipping publish to %s",
            stream,
        )
        return None
    try:
        msg_id = await client.stream_publish(stream, envelope)
    except Exception as e:
        # stream_publish swallows RedisError itself; this guards programming
        # errors (e.g. empty payload) so the caller's row never fails.
        log.error("[STREAM:EVENTS] publish to %s crashed: %s", stream, e)
        return None
    if msg_id is None:
        log.error(
            "[STREAM:EVENTS] publish to %s failed (Redis error) — event lost: "
            "type=%s org=%s",
            stream,
            envelope.get("type"),
            envelope.get("org_id"),
        )
    return msg_id


async def publish_image_processed(
    *,
    org_id: str,
    config_id: str,
    worksheet_id: int,
    row_id: int,
    attachment_id: Optional[int],
    column_id: Optional[int] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    """XADD an ``image_processed`` event to ``stream:platform-events``.

    Called once per successfully uploaded image. Best-effort — returns the
    stream message id, or ``None`` when Redis is not wired or the XADD
    failed (already logged).
    """
    envelope = {
        "v": ENVELOPE_VERSION,
        "type": "image_processed",
        "tool": TOOL_SLUG,
        "event_id": str(uuid.uuid4()),
        "org_id": str(org_id),
        "config_id": str(config_id),
        "worksheet_id": str(worksheet_id),
        "row_id": str(row_id),
        "attachment_id": str(attachment_id or ""),
        "column_id": str(column_id or ""),
        "filename": str(filename or ""),
        "ts": _now_iso(),
    }
    return await _publish(PLATFORM_EVENTS_STREAM, envelope)


async def publish_dlq_abandoned(
    *,
    org_id: str,
    worksheet_id: int,
    config_id: Optional[str],
    event_type: str,
    reason: str,
    dlq_id: Optional[str] = None,
    row_id: Optional[int] = None,
    error_type: str = "",
    retry_count: int = 0,
) -> Optional[str]:
    """XADD a permanent-failure event to ``stream:i2c:dlq``.

    Called when a Supabase DLQ entry is abandoned (retries exhausted or
    non-retryable). ``dlq_id`` links back to ``imagetocell_dlq`` so an
    operator or future consumer can fetch the full original payload.
    """
    envelope = {
        "v": ENVELOPE_VERSION,
        "type": "row_failed_permanent",
        "tool": TOOL_SLUG,
        "event_id": str(dlq_id or uuid.uuid4()),
        "org_id": str(org_id),
        "worksheet_id": str(worksheet_id),
        "config_id": str(config_id or ""),
        "row_id": str(row_id or ""),
        "source_event_type": str(event_type or ""),
        "reason": str(reason or "")[:1000],
        "error_type": str(error_type or ""),
        "retry_count": str(retry_count),
        "dlq_table": "imagetocell_dlq",
        "ts": _now_iso(),
    }
    return await _publish(I2C_DLQ_STREAM, envelope)
