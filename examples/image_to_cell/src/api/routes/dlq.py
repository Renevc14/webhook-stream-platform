"""
Dead Letter Queue Routes
========================

Endpoints for managing failed events and retries.
"""

import logging
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, status, Query

from src.services.supabase.dlq_repo import DeadLetterQueueRepository, FailedEvent
from src.api.dependencies import get_db, require_admin_key


log = logging.getLogger(__name__)

router = APIRouter(prefix="/dlq", tags=["Dead Letter Queue"])


def get_dlq_repo() -> DeadLetterQueueRepository:
    """Get DLQ repository dependency."""
    return DeadLetterQueueRepository()


@router.get("/stats")
async def get_dlq_stats(
    _admin: str = Depends(require_admin_key),
    dlq: DeadLetterQueueRepository = Depends(get_dlq_repo),
) -> Dict[str, Any]:
    """
    Get Dead Letter Queue statistics.

    Returns counts of events by status.
    """
    return {
        "stats": dlq.get_stats(),
    }


@router.get("/events")
async def get_pending_events(
    _admin: str = Depends(require_admin_key),
    limit: int = Query(default=50, ge=1, le=200),
    organization_id: str = Query(default=None),
    dlq: DeadLetterQueueRepository = Depends(get_dlq_repo),
) -> Dict[str, Any]:
    """
    Get pending failed events.

    Args:
        limit: Maximum events to return (1-200)
        organization_id: Optional filter by organization

    Returns:
        List of pending events
    """
    events = dlq.get_pending_events(limit=limit, organization_id=organization_id)

    return {
        "count": len(events),
        "events": [
            {
                "id": e.id,
                "organization_id": e.organization_id,
                "worksheet_id": e.worksheet_id,
                "event_type": e.event_type,
                "error_type": e.error_type,
                "error_message": e.error_message[:200] if e.error_message else None,
                "retry_count": e.retry_count,
                "max_retries": e.max_retries,
                "status": e.status,
                "created_at": e.created_at,
            }
            for e in events
        ],
    }


@router.post("/events/{event_id}/retry")
async def retry_event(
    event_id: str,
    _admin: str = Depends(require_admin_key),
    dlq: DeadLetterQueueRepository = Depends(get_dlq_repo),
) -> Dict[str, Any]:
    """
    Retry a single failed event.

    This marks the event as 'retrying' and attempts to reprocess it.
    """
    # Mark as retrying
    if not dlq.mark_as_retrying(event_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found or already processing"
        )

    # TODO: Implement actual retry logic by fetching the event
    # and reprocessing through the webhook handler

    # For now, just mark as resolved (placeholder)
    log.info(f"[DLQ] Retry requested for event {event_id}")

    return {
        "status": "retry_queued",
        "event_id": event_id,
        "message": "Event queued for retry",
    }


@router.post("/events/{event_id}/resolve")
async def resolve_event(
    event_id: str,
    _admin: str = Depends(require_admin_key),
    dlq: DeadLetterQueueRepository = Depends(get_dlq_repo),
) -> Dict[str, Any]:
    """
    Manually mark an event as resolved.

    Use this to clear events that have been handled manually
    or are no longer relevant.
    """
    if not dlq.mark_as_resolved(event_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found"
        )

    return {
        "status": "resolved",
        "event_id": event_id,
    }


@router.delete("/cleanup")
async def cleanup_old_events(
    _admin: str = Depends(require_admin_key),
    days: int = Query(default=30, ge=1, le=365),
    dlq: DeadLetterQueueRepository = Depends(get_dlq_repo),
) -> Dict[str, Any]:
    """
    Delete resolved events older than specified days.

    Args:
        days: Age threshold in days (1-365)
    """
    deleted = dlq.cleanup_old_resolved(days=days)

    return {
        "deleted_count": deleted,
        "message": f"Deleted {deleted} resolved events older than {days} days",
    }
