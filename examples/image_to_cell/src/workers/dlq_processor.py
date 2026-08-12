"""
Dead Letter Queue Processor
===========================

Background worker that retries failed events from the DLQ.
Runs periodically to process pending events with exponential backoff.
"""

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from src.config.settings import Settings, get_settings
from src.services.supabase.dlq_repo import DeadLetterQueueRepository, FailedEvent
from src.services.supabase.config_repo import ConfigRepository
from src.services.smartsheet.client import get_smartsheet_client
from src.services.smartsheet.sheets import SheetService
from src.services.smartsheet.attachments import AttachmentService
from src.core.processor import ImageProcessor
from src.services.stream_events import (
    publish_dlq_abandoned,
    publish_image_processed,
)
from src.services.usage_log import log_image_conversion as log_usage
from src.utils.logging import FlowLogger, log_context, log_separator


log = logging.getLogger(__name__)


@dataclass
class DLQProcessorResult:
    """Result of a DLQ processing cycle."""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events_processed: int = 0
    events_resolved: int = 0
    events_failed: int = 0
    events_abandoned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "events_processed": self.events_processed,
            "events_resolved": self.events_resolved,
            "events_failed": self.events_failed,
            "events_abandoned": self.events_abandoned,
        }


class DLQProcessor:
    """
    Background worker that processes failed events from the DLQ.

    Features:
    - Periodic processing of pending events
    - Exponential backoff for retries
    - Max retry limit before abandoning
    - Batch processing with concurrency limits
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        interval_seconds: int = 300,  # 5 minutes
        batch_size: int = 20,
    ):
        self._settings = settings or get_settings()
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._running = False
        self._last_result: Optional[DLQProcessorResult] = None

        self._dlq_repo = DeadLetterQueueRepository()
        self._config_repo = ConfigRepository()
        self._processor = ImageProcessor()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_result(self) -> Optional[DLQProcessorResult]:
        return self._last_result

    async def start(self) -> None:
        """Start the DLQ processor background loop."""
        if self._running:
            log.warning("[DLQ:PROCESSOR] Already running")
            return

        self._running = True
        log.info("[DLQ:PROCESSOR] Starting DLQ processor")
        log.info(f"[DLQ:PROCESSOR] Interval: {self._interval}s, Batch size: {self._batch_size}")

        # Wait briefly before first run to let system stabilize
        await asyncio.sleep(10)

        lock_path = os.path.join(tempfile.gettempdir(), "i2c_dlq_processor.lock")
        cycle_count = 0

        while self._running:
            try:
                from filelock import FileLock, Timeout
                lock = FileLock(lock_path, timeout=0)
                try:
                    with lock:
                        result = await self.process_batch()
                        self._last_result = result

                        if result.events_processed > 0:
                            log.info(
                                f"[DLQ:PROCESSOR] Cycle complete: "
                                f"{result.events_resolved} resolved, "
                                f"{result.events_failed} failed, "
                                f"{result.events_abandoned} abandoned"
                            )

                        # Cleanup old resolved/abandoned events every 12 cycles (~1 hour)
                        cycle_count += 1
                        if cycle_count % 12 == 0:
                            try:
                                self._dlq_repo.cleanup_old_resolved(days=30)
                                self._dlq_repo.cleanup_old_abandoned(days=7)
                            except Exception as cleanup_err:
                                log.warning(f"[DLQ:PROCESSOR] Cleanup failed (non-critical): {cleanup_err}")
                except Timeout:
                    log.debug("[DLQ:PROCESSOR] Another worker is processing DLQ, skipping")

            except Exception as e:
                log.error(f"[DLQ:PROCESSOR] Processing cycle failed: {e}")

            if self._running:
                await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        """Stop the DLQ processor."""
        log.info("[DLQ:PROCESSOR] Stopping...")
        self._running = False

    async def process_batch(self) -> DLQProcessorResult:
        """
        Process a batch of pending events in parallel.

        Returns:
            DLQProcessorResult with processing statistics
        """
        result = DLQProcessorResult()

        # Get pending events
        events = self._dlq_repo.get_pending_events(limit=self._batch_size)

        if not events:
            return result

        log.info(f"[DLQ:PROCESSOR] Processing {len(events)} pending events in parallel")
        result.events_processed = len(events)

        async def _process_one(event: FailedEvent) -> str:
            """Retry a single event, return outcome: 'resolved', 'abandoned', or 'failed'."""
            try:
                success = await self._retry_event(event)
                if success:
                    self._dlq_repo.mark_as_resolved(event.id)
                    return "resolved"
                else:
                    self._dlq_repo.increment_retry_count(event.id)
                    if event.retry_count + 1 >= event.max_retries:
                        # SH-155: retries exhausted — the failure is now
                        # permanent, surface it on the stream DLQ.
                        await self._publish_abandoned(
                            event,
                            reason=event.error_message
                            or "max retries exhausted",
                        )
                        return "abandoned"
                    return "failed"
            except Exception as e:
                log.error(f"[DLQ:PROCESSOR] Failed to retry event {event.id}: {e}")
                self._dlq_repo.increment_retry_count(event.id, error=e)
                if event.retry_count + 1 >= event.max_retries:
                    # SH-155: this crash consumed the last retry —
                    # increment_retry_count just marked the row abandoned.
                    await self._publish_abandoned(event, reason=str(e))
                return "failed"

        outcomes = await asyncio.gather(*[_process_one(e) for e in events])

        for outcome in outcomes:
            if outcome == "resolved":
                result.events_resolved += 1
            elif outcome == "abandoned":
                result.events_abandoned += 1
            else:
                result.events_failed += 1

        return result

    async def _publish_abandoned(self, event: FailedEvent, reason: str) -> None:
        """SH-155: mirror a permanent DLQ failure onto ``stream:i2c:dlq``.

        Best-effort — the Supabase row is already marked abandoned and stays
        the source of truth; the stream entry is observability metadata.
        """
        event_data = event.event_data or {}
        await publish_dlq_abandoned(
            org_id=event.organization_id,
            worksheet_id=event.worksheet_id,
            config_id=event.config_id,
            event_type=event.event_type,
            reason=reason,
            dlq_id=event.id,
            row_id=event_data.get("row_id"),
            error_type=event.error_type,
            retry_count=event.retry_count,
        )

    async def _retry_event(self, event: FailedEvent) -> bool:
        """
        Retry a single failed event.

        Args:
            event: The failed event to retry

        Returns:
            True if retry succeeded, False otherwise
        """
        flow = FlowLogger(
            log, "DLQ:RETRY",
            event_id=event.id[:8] if event.id else "unknown",
            retry=event.retry_count + 1,
        )

        flow.start(f"Retrying {event.event_type} event")

        # Mark as retrying
        self._dlq_repo.mark_as_retrying(event.id)

        try:
            # Get the config
            config = None
            if event.config_id:
                config = self._config_repo.get_by_id(event.config_id)
                if not config:
                    # Config deleted — try any active config on the same worksheet as fallback
                    # (e.g., if config was deleted and recreated with a new ID)
                    flow.warning(
                        "Config not found, looking for active config on worksheet"
                    )
                    fallback = self._config_repo.get_by_worksheet(event.worksheet_id)
                    if fallback:
                        config = fallback[0]
                        flow.warning(f"Using fallback config {config.id} for worksheet {event.worksheet_id}")
                    else:
                        flow.warning("No active config for worksheet, abandoning event")
                        reason = (
                            "Config not found and no active config for worksheet"
                        )
                        self._dlq_repo.mark_as_abandoned(event.id, reason=reason)
                        # SH-155: permanent failure — surface on stream DLQ.
                        await self._publish_abandoned(event, reason=reason)
                        return True  # Signal success so process_batch doesn't increment retry count

            if not config:
                flow.warning("No valid config found for event")
                return False

            # Process based on event type
            if event.event_type == "attachment_created":
                success = await self._retry_attachment_event(event, config)
                if success:
                    flow.success("Event resolved successfully")
                else:
                    flow.failure("Event retry failed")
                return success

            else:
                flow.warning(f"Unknown event type: {event.event_type}")
                return False

        except Exception as e:
            flow.failure("Retry failed with exception", e)
            raise

    async def _retry_attachment_event(
        self,
        event: FailedEvent,
        config,
    ) -> bool:
        """Retry an attachment creation event."""
        event_data = event.event_data or {}
        row_id = event_data.get("row_id")

        # Hotfix 2026-06-17 (Beth incident): the row-batch path in
        # `webhook_handler.py` stores `attachment_ids` (plural, list) while
        # the single-event path stores `attachment_id` (singular, scalar).
        # The previous reader only handled the singular shape, so every
        # row-batch DLQ entry was abandoned with "No attachment_id in event
        # data" and never retried — exactly the bug Beth hit.
        #
        # Now we accept both. `process_row` always processes the whole row's
        # attachments anyway, so the only thing we actually need is `row_id`
        # (or any one attachment_id to look it up). Iterating the plural
        # list isn't necessary at this layer.
        attachment_ids = event_data.get("attachment_ids") or []
        if not isinstance(attachment_ids, list):
            # Defensive: writer could in theory store a scalar by mistake.
            attachment_ids = [attachment_ids] if attachment_ids else []
        # Singular field stays supported for legacy entries that pre-date
        # the row-batch path.
        singular = event_data.get("attachment_id")
        if singular:
            attachment_ids = [*attachment_ids, singular]
        attachment_ids = [a for a in attachment_ids if a]

        if not row_id and not attachment_ids:
            log.warning(
                "[DLQ:RETRY] No row_id and no attachment_id(s) in event data: %s",
                event_data,
            )
            return False

        with log_context(
            org_id=config.organization_id,
            worksheet_id=config.worksheet_id,
        ):
            try:
                # SH-100: both SheetService and AttachmentService are now async,
                # backed by smarthub_core's SmartsheetClient.
                # SH-178: DLQ retries run as the config's runner.
                if getattr(config, "needs_connection", False):
                    log.warning(
                        "[DLQ:RETRY] SH-178: config %s needs_connection — "
                        "skipping until a runner reconnects",
                        config.id,
                    )
                    return False

                client = get_smartsheet_client(
                    config.organization_id, config.runner
                )
                try:
                    # Force credential resolve so revoke surfaces before work.
                    await client.get_client(force_refresh=True)
                except Exception as cred_exc:
                    from src.services.runner_failover import (
                        classify_credential_failure,
                        failover_configs,
                    )

                    if classify_credential_failure(cred_exc) == "reconnect_required":
                        log.warning(
                            "[DLQ:RETRY] SH-178: runner revoked for config %s — "
                            "failover",
                            config.id,
                        )
                        await failover_configs(
                            [
                                {
                                    "id": config.id,
                                    "organization_id": config.organization_id,
                                }
                            ],
                            organization_id=config.organization_id,
                            revoked_runner_user_id=config.runner,
                        )
                        return False
                    raise
                attachment_service = AttachmentService(client)
                sheet_service = SheetService(client)

                # If we don't have row_id, resolve it from any of the
                # attachment_ids we do have. Walk the list so a single
                # already-deleted attachment doesn't sink the whole retry.
                if not row_id:
                    for att_id in attachment_ids:
                        try:
                            att_meta = await attachment_service.get_attachment_metadata(
                                config.worksheet_id,
                                att_id,
                            )
                        except Exception as lookup_err:
                            log.debug(
                                "[DLQ:RETRY] metadata lookup failed for att=%s: %s",
                                att_id, lookup_err,
                            )
                            continue
                        if (att_meta.get("parentType") or "").lower() != "row":
                            continue
                        candidate = att_meta.get("parentId")
                        if candidate:
                            row_id = candidate
                            break

                if not row_id:
                    log.warning(
                        "[DLQ:RETRY] Could not determine row_id from any of "
                        "%d attachment_id(s)",
                        len(attachment_ids),
                    )
                    return False

                # Fetch the row (SH-100: direct await on async wrapper)
                row = await sheet_service.get_row(
                    config.worksheet_id,
                    row_id,
                    include_attachments=True,
                )

                # Process the row (skip usage logging — we handle it below
                # to avoid double-counting failures already recorded).
                # SH-101: processor.process_row is async-first.
                result = await self._processor.process_row(
                    config, row, skip_usage_logging=True
                )

                # Log usage only for images that succeeded on this retry.
                # Failures were already logged during the original attempt.
                if result.image_results:
                    user_id = getattr(config, "created_by", None) or getattr(config, "user_id", None)
                    for img_result in result.image_results:
                        if img_result.success:
                            col_name = None
                            try:
                                col_name = config.get_column_name(img_result.column_id) if img_result.column_id else None
                            except Exception:
                                pass
                            log_usage(
                                organization_id=config.organization_id,
                                user_id=user_id,
                                config_id=config.id,
                                config_name=config.configuration_name,
                                config_type=config.config_type.value,
                                worksheet_id=config.worksheet_id,
                                worksheet_name=config.worksheet_name,
                                row_id=row_id,
                                image_column_id=img_result.column_id or 0,
                                image_column_name=col_name,
                                image_filename=img_result.filename,
                                success=True,
                                image_size_bytes=img_result.original_size_bytes or None,
                                processing_duration_ms=img_result.processing_time_ms or None,
                                error_message=None,
                                is_system_generated=False,
                            )
                            # SH-155: DLQ retries skip the in-process_row
                            # publish (skip_usage_logging=True), so emit the
                            # platform event here alongside the usage log.
                            await publish_image_processed(
                                org_id=config.organization_id,
                                config_id=config.id,
                                worksheet_id=config.worksheet_id,
                                row_id=row_id,
                                attachment_id=None,
                                column_id=img_result.column_id,
                                filename=img_result.filename,
                            )

                log.info(
                    f"[DLQ:RETRY] Processed row {row_id}: "
                    f"{result.images_processed} processed, {result.images_failed} failed"
                )

                # Only mark as resolved if no images failed;
                # otherwise let DLQ retry again for the remaining failures
                return result.images_processed > 0 or result.images_failed == 0

            except Exception as e:
                error_str = str(e)
                log.error(f"[DLQ:RETRY] Failed to process attachment: {error_str}")

                # Detect non-retryable Smartsheet errors (404, shouldRetry: false)
                try:
                    import json as _json
                    error_data = _json.loads(error_str)
                    result_data = error_data.get("result", {})
                    if result_data.get("shouldRetry") is False or result_data.get("statusCode") == 404:
                        log.warning(
                            f"[DLQ:RETRY] Non-retryable Smartsheet error "
                            f"(code={result_data.get('statusCode')}, shouldRetry=false) — abandoning event"
                        )
                        self._dlq_repo.mark_as_abandoned(event.id, reason=error_str[:500])
                        # SH-155: permanent failure — surface on stream DLQ.
                        await self._publish_abandoned(
                            event, reason=error_str[:500]
                        )
                        return True  # Return True so process_batch doesn't increment retry count
                except Exception:
                    pass  # Not a JSON Smartsheet error, fall through to normal retry

                return False


# Singleton
_dlq_processor: Optional[DLQProcessor] = None


def get_dlq_processor() -> DLQProcessor:
    """Get the global DLQ processor singleton."""
    global _dlq_processor
    if _dlq_processor is None:
        _dlq_processor = DLQProcessor()
    return _dlq_processor


async def start_dlq_processor() -> None:
    """Start the DLQ processor background worker."""
    processor = get_dlq_processor()
    await processor.start()


async def stop_dlq_processor() -> None:
    """Stop the DLQ processor."""
    processor = get_dlq_processor()
    await processor.stop()
