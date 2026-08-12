"""
Dead Letter Queue Repository
============================

Stores and retrieves failed webhook events for later reprocessing.
"""

import logging
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict


class DLQJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles datetime objects."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

from src.config.constants import TABLE_NAMES
from src.services.supabase.client import SupabaseService, get_supabase_service


log = logging.getLogger(__name__)


@dataclass
class FailedEvent:
    """A failed webhook event stored in the DLQ."""
    id: Optional[str] = None
    organization_id: str = ""
    worksheet_id: int = 0
    config_id: Optional[str] = None
    event_type: str = ""
    event_data: Dict[str, Any] = None
    error_message: str = ""
    error_type: str = ""
    retry_count: int = 0
    max_retries: int = 3
    status: str = "pending"  # pending, retrying, resolved, abandoned
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_retry_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        data = {
            "organization_id": self.organization_id,
            "worksheet_id": self.worksheet_id,
            "config_id": self.config_id,
            "event_type": self.event_type,
            "event_data": json.dumps(self.event_data, cls=DLQJSONEncoder) if self.event_data else "{}",
            "error_message": self.error_message[:1000],  # Truncate long errors
            "error_type": self.error_type,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "status": self.status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.last_retry_at:
            data["last_retry_at"] = self.last_retry_at
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FailedEvent":
        """Create from database row."""
        event_data = data.get("event_data", "{}")
        if isinstance(event_data, str):
            try:
                event_data = json.loads(event_data)
            except json.JSONDecodeError:
                event_data = {}

        return cls(
            id=data.get("id"),
            organization_id=data.get("organization_id", ""),
            worksheet_id=data.get("worksheet_id", 0),
            config_id=data.get("config_id"),
            event_type=data.get("event_type", ""),
            event_data=event_data,
            error_message=data.get("error_message", ""),
            error_type=data.get("error_type", ""),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            status=data.get("status", "pending"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            last_retry_at=data.get("last_retry_at"),
        )


class DeadLetterQueueRepository:
    """
    Repository for managing failed events (Dead Letter Queue).

    Failed events are stored for later manual or automatic retry.
    """

    def __init__(self, supabase: Optional[SupabaseService] = None):
        self._db = supabase or get_supabase_service()

    def store_failed_event(
        self,
        organization_id: str,
        worksheet_id: int,
        event_type: str,
        event_data: Dict[str, Any],
        error: Exception,
        config_id: Optional[str] = None,
        max_retries: int = 3,
    ) -> Optional[str]:
        """
        Store a failed event in the DLQ.

        Args:
            organization_id: Organization ID
            worksheet_id: Worksheet ID
            event_type: Type of event (e.g., "attachment_created")
            event_data: Original event data
            error: The exception that caused the failure
            config_id: Optional config ID
            max_retries: Maximum retry attempts

        Returns:
            ID of the stored event or None on failure
        """
        try:
            failed_event = FailedEvent(
                organization_id=organization_id,
                worksheet_id=worksheet_id,
                config_id=config_id,
                event_type=event_type,
                event_data=event_data,
                error_message=str(error),
                error_type=type(error).__name__,
                max_retries=max_retries,
            )

            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .insert(failed_event.to_dict())
                    .execute(),
                "store failed event"
            )

            if result.data:
                event_id = result.data[0].get("id")
                log.warning(
                    f"[DLQ] Stored failed event {event_id}: "
                    f"{event_type} for worksheet {worksheet_id}"
                )
                return event_id

        except Exception as e:
            log.error(f"[DLQ] Failed to store event in DLQ: {e}")

        return None

    def get_pending_events(
        self,
        limit: int = 100,
        organization_id: Optional[str] = None,
    ) -> List[FailedEvent]:
        """
        Get pending events ready for retry.

        Args:
            limit: Maximum events to return
            organization_id: Optional filter by organization

        Returns:
            List of pending FailedEvent objects
        """
        try:
            query = (
                self._db.table(TABLE_NAMES.FAILED_EVENTS)
                .select("*")
                .eq("status", "pending")
                .lt("retry_count", 3)  # Below max retries
                .order("created_at")
                .limit(limit)
            )

            if organization_id:
                query = query.eq("organization_id", organization_id)

            result = self._db.execute_with_retry(
                lambda: query.execute(),
                "get pending events"
            )

            return [FailedEvent.from_dict(row) for row in (result.data or [])]

        except Exception as e:
            log.error(f"[DLQ] Failed to get pending events: {e}")
            return []

    def mark_as_retrying(self, event_id: str) -> bool:
        """Mark an event as currently being retried."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .update({
                        "status": "retrying",
                        "last_retry_at": now,
                        "updated_at": now,
                    })
                    .eq("id", event_id)
                    .execute(),
                f"mark event {event_id} as retrying"
            )
            return True
        except Exception as e:
            log.error(f"[DLQ] Failed to mark event as retrying: {e}")
            return False

    def mark_as_abandoned(self, event_id: str, reason: str = "") -> bool:
        """Mark an event as permanently abandoned (non-retryable error)."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            update = {"status": "abandoned", "updated_at": now}
            if reason:
                update["error_message"] = reason[:1000]
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .update(update)
                    .eq("id", event_id)
                    .execute(),
                f"mark event {event_id} as abandoned"
            )
            log.warning(f"[DLQ] Event {event_id} abandoned: {reason}")
            return True
        except Exception as e:
            log.error(f"[DLQ] Failed to mark event as abandoned: {e}")
            return False

    def mark_as_resolved(self, event_id: str) -> bool:
        """Mark an event as successfully resolved."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .update({
                        "status": "resolved",
                        "updated_at": now,
                    })
                    .eq("id", event_id)
                    .execute(),
                f"mark event {event_id} as resolved"
            )
            log.info(f"[DLQ] Event {event_id} resolved successfully")
            return True
        except Exception as e:
            log.error(f"[DLQ] Failed to mark event as resolved: {e}")
            return False

    def increment_retry_count(
        self,
        event_id: str,
        error: Optional[Exception] = None,
    ) -> bool:
        """
        Increment retry count and update status.

        If max retries exceeded, marks as abandoned.
        """
        try:
            # First get current state
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .select("retry_count, max_retries")
                    .eq("id", event_id)
                    .single()
                    .execute(),
                f"get event {event_id}"
            )

            if not result.data:
                return False

            current_retry = result.data.get("retry_count", 0)
            max_retries = result.data.get("max_retries", 3)
            new_retry = current_retry + 1

            # Determine new status
            if new_retry >= max_retries:
                new_status = "abandoned"
                log.warning(f"[DLQ] Event {event_id} abandoned after {new_retry} retries")
            else:
                new_status = "pending"

            now = datetime.now(timezone.utc).isoformat()
            update_data = {
                "retry_count": new_retry,
                "status": new_status,
                "updated_at": now,
            }

            if error:
                update_data["error_message"] = str(error)[:1000]
                update_data["error_type"] = type(error).__name__

            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .update(update_data)
                    .eq("id", event_id)
                    .execute(),
                f"increment retry for {event_id}"
            )
            return True

        except Exception as e:
            log.error(f"[DLQ] Failed to increment retry count: {e}")
            return False

    def get_stats(self) -> Dict[str, int]:
        """Get DLQ statistics."""
        stats = {
            "pending": 0,
            "retrying": 0,
            "resolved": 0,
            "abandoned": 0,
            "total": 0,
        }

        try:
            for status in ["pending", "retrying", "resolved", "abandoned"]:
                result = self._db.execute_with_retry(
                    lambda s=status: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                        .select("id", count="exact")
                        .eq("status", s)
                        .execute(),
                    f"count {status} events"
                )
                count = result.count if hasattr(result, 'count') else len(result.data or [])
                stats[status] = count
                stats["total"] += count

        except Exception as e:
            log.error(f"[DLQ] Failed to get stats: {e}")

        return stats

    def cleanup_old_resolved(self, days: int = 30) -> int:
        """
        Delete resolved events older than specified days.

        Returns:
            Number of deleted events
        """
        try:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .delete()
                    .eq("status", "resolved")
                    .lt("updated_at", cutoff)
                    .execute(),
                "cleanup old resolved events"
            )

            deleted = len(result.data or [])
            if deleted:
                log.info(f"[DLQ] Cleaned up {deleted} old resolved events")
            return deleted

        except Exception as e:
            log.error(f"[DLQ] Failed to cleanup old events: {e}")
            return 0

    def cleanup_old_abandoned(self, days: int = 7) -> int:
        """
        Delete abandoned events older than specified days.

        Abandoned events are permanently failed and safe to remove after
        a short retention window for debugging purposes.

        Returns:
            Number of deleted events
        """
        try:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.FAILED_EVENTS)
                    .delete()
                    .eq("status", "abandoned")
                    .lt("updated_at", cutoff)
                    .execute(),
                "cleanup old abandoned events"
            )

            deleted = len(result.data or [])
            if deleted:
                log.info(f"[DLQ] Cleaned up {deleted} old abandoned events")
            return deleted

        except Exception as e:
            log.error(f"[DLQ] Failed to cleanup old abandoned events: {e}")
            return 0
