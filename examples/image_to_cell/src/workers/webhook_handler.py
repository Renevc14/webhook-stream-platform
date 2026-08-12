"""
Webhook Handler
===============

Handles real-time webhook events from Smartsheet.

Based on v2 robustness patterns for signature verification.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import logging
import time
from typing import Optional, Dict, Any, List, Union
from dataclasses import dataclass

from src.workers.base import BaseWorker
from src.core.processor import ImageProcessor
from src.models.webhook import WebhookPayload, WebhookEvent
from src.models.results import WebhookResult
from src.models.attachment import safe_parse_timestamp
from src.services.supabase.webhook_repo import WebhookRepository
from src.services.supabase.config_repo import ConfigRepository
from src.services.supabase.dlq_repo import DeadLetterQueueRepository
from src.services.smartsheet.client import get_smartsheet_client
from src.services.smartsheet.sheets import SheetService
from src.services.smartsheet.attachments import AttachmentService
from src.services.webhook_dedup import WebhookDedupService, get_dedup_service
from src.config.settings import Settings
from src.config.constants import CHANGE_AGENT_ID
from src.utils.logging import FlowLogger, log_context, FlowTags


log = logging.getLogger(__name__)

# SH-191: form multi-file uploads often share the same createdAt second (or
# stagger by ~1s) while Smartsheet only emits one attachment.created event.
# Expand SH-109 triggers to siblings within this window. Hours/days-old
# SH-107 "ghost" attachments stay outside the window and remain excluded.
_COARRIVAL_WINDOW_S = 5.0


@dataclass
class SignatureVerificationResult:
    """
    Result of signature verification with detailed status.

    Based on v2 pattern for debugging and handling edge cases.
    """
    ok: bool
    status: str  # "valid", "invalid", "no_secret", "zombie_webhook", "superseded_webhook_event"
    message: Optional[str] = None

    def __bool__(self) -> bool:
        return self.ok


class WebhookHandler(BaseWorker):
    """
    Handler for real-time Smartsheet webhook events.

    Processes attachment events and triggers image processing.
    """

    def __init__(
        self,
        processor: Optional[ImageProcessor] = None,
        settings: Optional[Settings] = None,
        dedup_service: Optional[WebhookDedupService] = None,
    ):
        """
        Initialize webhook handler.

        Args:
            processor: Image processor
            settings: Application settings
            dedup_service: SH-99 cross-process dedup service. Injectable for
                tests; defaults to the module singleton wired by app.lifespan.
        """
        super().__init__(processor, settings)
        self._webhook_repo = WebhookRepository()
        self._config_repo = ConfigRepository()
        self._dlq_repo = DeadLetterQueueRepository()
        max_concurrent = settings.max_concurrent_attachments if settings else 10
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # SH-102: dropped `self._executor` (ThreadPoolExecutor) and
        # `self._db_semaphore` (threading.Semaphore) along with the
        # `_get_ss_limited` helper. Every Smartsheet call now goes through
        # the smarthub_core SmartsheetClient — token resolution is a Redis
        # cache-aside, rate limiting is cluster-wide, and the wrappers are
        # async, so there is no longer a blocking SDK path to wrap.
        # SH-99: cross-process dedup. Replaces the legacy in-memory
        # `_processing_cache` (per attachment:row in-flight slot) and
        # `_completed_attachments` (per attachment_id post-completion skip).
        # Both are now keyed in Redis (`dedup:i2c:processing:*` and
        # `dedup:i2c:completed:*`) so a webhook retry that lands on a
        # different ECS task — or a task that restarts mid-burst — does
        # not re-process. Falls back to in-memory dicts inside the
        # service if Redis is unavailable.
        # SH-110: `_row_cache` was removed; row-level dedup is covered by
        # `state.last_ids` in RowState, the SH-98 UNIQUE constraint, and
        # SH-109 webhook scoping. Not migrated to Redis.
        self._dedup = dedup_service or get_dedup_service()
        # Max age for events to process (ignore old backlog)
        self._max_event_age_minutes = 5

    async def start(self) -> None:
        """Start is not applicable - webhooks are event-driven."""
        raise NotImplementedError(
            "WebhookHandler is event-driven. Use handle_webhook() instead."
        )

    async def stop(self) -> None:
        """Stop the handler."""
        self._running = False
        self._log_stop()

    async def _resolve_attachment_parent_rows(
        self,
        events: List["WebhookEvent"],
        configs: list,
        worksheet_id: int,
    ) -> None:
        """
        SH-110: populate `event.row_id` for attachment.created events that
        Smartsheet did not include a parent rowId for.

        Without this step, the top-level grouping by row_id treats every
        such event as "row 0 (unknown)", fans out to the per-event path,
        and races on `_completed_attachments` cache so only the first
        event survives — silently losing the other concurrent uploads on
        the same row.

        Strategy: round-robin across the unique organization_ids in
        `configs` so the rate-limit cost of the metadata fetches is
        distributed fairly across orgs. The attachment_id is global to
        Smartsheet, so any org with access to the sheet returns the same
        `parentId` — we only need ONE successful resolution per event.
        Failures are tolerated (event keeps row_id=0 and falls back to
        the legacy unknown-row path).
        """
        unresolved = [
            e for e in events
            if not getattr(e, "row_id", 0)
            and getattr(e.object_type, "value", str(e.object_type)).lower() == "attachment"
            and getattr(e.event_type, "value", str(e.event_type)).lower() == "created"
            and getattr(e, "object_id", None)
        ]
        if not unresolved:
            return

        # Round-robin Smartsheet clients across unique organizations.
        # SH-100: AttachmentService is now async + uses smarthub_core's
        # SmartsheetClient (token cache + cluster-wide rate limit), so the
        # threadpool / token-lookup semaphore wrap is no longer needed.
        # Map each org to one of its config runners so the token resolver can
        # prefer that user's per-tool OAuth token (US/EU plans).
        # SH-178: runner identity (runner_user_id, created_by fallback).
        org_owner = {c.organization_id: c.runner for c in configs}
        org_ids = list(org_owner)
        if not org_ids:
            return
        clients = [
            AttachmentService(get_smartsheet_client(o, org_owner[o]))
            for o in org_ids
        ]

        async def _resolve(idx: int, event):
            svc = clients[idx % len(clients)]
            try:
                meta = await svc.get_attachment_metadata(
                    worksheet_id, event.object_id
                )
            except Exception as e:
                log.info(
                    "[SH-110] could not resolve parent for attachment=%s: %s",
                    event.object_id, e,
                )
                return
            parent_type = (meta.get("parentType") or "").lower()
            parent_id = meta.get("parentId")
            if parent_type == "row" and parent_id:
                event.row_id = int(parent_id)

        await asyncio.gather(
            *[_resolve(i, e) for i, e in enumerate(unresolved)],
            return_exceptions=True,
        )

        resolved_count = sum(1 for e in unresolved if e.row_id)
        if resolved_count:
            log.info(
                "[SH-110] pre-resolved parent row_id for %d/%d attachment event(s) "
                "using %d organization(s)",
                resolved_count, len(unresolved), len(org_ids),
            )

    @staticmethod
    def _scope_row_to_trigger_attachments(
        row: Dict[str, Any],
        trigger_attachment_ids: set,
    ) -> Dict[str, Any]:
        """
        SH-109: return the row with `attachments` narrowed to only those
        whose id is in `trigger_attachment_ids`.

        Webhooks fire per-attachment event. A naive process_row call would
        consider EVERY attachment on the row, including old ones that were
        intentionally skipped before (e.g. by SH-105 anti-dup). When the
        user later clears the cell that anti-dup matched and uploads a new
        file, the new webhook would silently re-process the old skipped
        attachment too — billing the customer for something they never
        asked to be auto-placed.

        Scoping to the trigger attachment(s) only matches the user's mental
        model ("you uploaded this file → it gets processed") and keeps
        backfill responsible for re-processing legitimately empty cells.

        `cells` is preserved untouched — SH-105/SH-108 still inspect them
        to decide whether the target slot is filled and which signatures
        in row_state are stale.
        """
        if not trigger_attachment_ids:
            return row
        kept = [
            a for a in row.get("attachments", []) or []
            if a.get("id") in trigger_attachment_ids
        ]
        dropped = len((row.get("attachments") or [])) - len(kept)
        if dropped > 0:
            log.info(
                "[SH-109] row=%s scoped to %d trigger attachment(s), ignored %d unrelated row attachment(s)",
                row.get("id"), len(kept), dropped,
            )
        out = dict(row)
        out["attachments"] = kept
        return out

    @staticmethod
    def _expand_triggers_with_coarrived(
        row: Dict[str, Any],
        trigger_attachment_ids: set,
        window_s: float = _COARRIVAL_WINDOW_S,
    ) -> set:
        """
        SH-191: expand webhook trigger IDs to include row attachments that
        arrived in the same co-arrival window (same form multi-file batch).

        Smartsheet form uploads often place N files on the row but emit only
        one ``attachment.created`` event. Without expansion, SH-109 would
        drop the siblings. Attachments whose ``createdAt`` cannot be parsed,
        or that fall outside ``window_s`` of every trigger timestamp, are
        left out — preserving SH-107 (old ghost attachments stay excluded).

        Fail closed: if no trigger attachment has a parseable timestamp,
        return the original trigger set unchanged.
        """
        if not trigger_attachment_ids:
            return set(trigger_attachment_ids)

        attachments = row.get("attachments") or []
        if not attachments:
            return set(trigger_attachment_ids)

        id_to_ts: Dict[int, float] = {}
        for att in attachments:
            att_id = att.get("id")
            if att_id is None:
                continue
            parsed = safe_parse_timestamp(att.get("createdAt"))
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            id_to_ts[int(att_id)] = parsed.timestamp()

        trigger_ts = [
            id_to_ts[tid]
            for tid in trigger_attachment_ids
            if tid in id_to_ts
        ]
        if not trigger_ts:
            return set(trigger_attachment_ids)

        t_min = min(trigger_ts) - window_s
        t_max = max(trigger_ts) + window_s
        expanded = set(trigger_attachment_ids)
        for att_id, ts in id_to_ts.items():
            if t_min <= ts <= t_max:
                expanded.add(att_id)

        if len(expanded) > len(trigger_attachment_ids):
            log.info(
                "[SH-191] row=%s expanded triggers %d -> %d "
                "(co-arrival window=+/-%ss)",
                row.get("id"),
                len(trigger_attachment_ids),
                len(expanded),
                window_s,
            )
        return expanded

    async def process_once(self) -> WebhookResult:
        """Not applicable for webhook handler."""
        raise NotImplementedError(
            "WebhookHandler is event-driven. Use handle_webhook()."
        )

    async def verify_signature(
        self,
        payload_body: bytes,
        signature: str,
        worksheet_id: int,
        webhook_id: Optional[str] = None,
    ) -> Union[bool, SignatureVerificationResult]:
        """
        Verify webhook signature with multi-method lookup.

        Based on v2 robustness patterns:
        1. Try to get secret by worksheet_id
        2. If not found, try by webhook_id
        3. If not found, try inline recovery from Smartsheet API
        4. Detect zombie webhooks (no active config)
        5. Detect superseded webhooks (webhook_id mismatch)

        Args:
            payload_body: Raw request body
            signature: Signature from header
            worksheet_id: Worksheet ID to look up secret
            webhook_id: Optional webhook ID for fallback lookup

        Returns:
            SignatureVerificationResult with detailed status
            (Also works as bool for backward compatibility)
        """
        webhook_secret = None
        db_webhook_id = None

        # Method 1: Try to get secret by worksheet_id
        try:
            webhook_reg = self._webhook_repo.get_by_worksheet(worksheet_id)
            if webhook_reg and webhook_reg.shared_secret:
                webhook_secret = webhook_reg.shared_secret
                db_webhook_id = webhook_reg.webhook_id
                log.debug(f"[VERIFY] Retrieved secret by worksheet_id {worksheet_id}")

                # Check for superseded webhook (based on v2)
                if webhook_id and str(db_webhook_id) != str(webhook_id):
                    log.warning(
                        f"[VERIFY] SUPERSEDED WEBHOOK: Event from webhook {webhook_id} "
                        f"but database has {db_webhook_id} for worksheet {worksheet_id}"
                    )
                    return SignatureVerificationResult(
                        ok=False,
                        status="superseded_webhook_event",
                        message=f"Event from old webhook {webhook_id}, current is {db_webhook_id}"
                    )
        except Exception as e:
            log.debug(f"[VERIFY] Could not get secret by worksheet_id: {e}")

        # Method 2: Try to get secret by webhook_id if not found
        if not webhook_secret and webhook_id:
            try:
                webhook_reg = self._webhook_repo.get_by_webhook_id(int(webhook_id))
                if webhook_reg and webhook_reg.shared_secret:
                    webhook_secret = webhook_reg.shared_secret
                    log.debug(f"[VERIFY] Retrieved secret by webhook_id {webhook_id}")
            except Exception as e:
                log.debug(f"[VERIFY] Could not get secret by webhook_id: {e}")

        # Method 3: Inline recovery from Smartsheet API (based on v2)
        if not webhook_secret and webhook_id:
            try:
                # Check if there's an active config for this worksheet
                configs = self._config_repo.get_active_webhook_configs(worksheet_id, modes=self._settings.webhook_processing_modes_list)
                if not configs:
                    log.warning(
                        f"[VERIFY] ZOMBIE WEBHOOK: Webhook {webhook_id} for worksheet "
                        f"{worksheet_id} has no active config"
                    )
                    return SignatureVerificationResult(
                        ok=False,
                        status="zombie_webhook",
                        message=f"No active config for worksheet {worksheet_id}"
                    )

                # SH-102: inline recovery now uses the async
                # SmartsheetWebhookService → SmartsheetClient (encrypted
                # token cache + cluster-wide rate limit + retry coverage).
                from src.services.smartsheet.webhooks import (
                    SmartsheetWebhookService,
                )

                org_id = configs[0].organization_id
                webhook_svc = SmartsheetWebhookService(
                    get_smartsheet_client(org_id, configs[0].runner)
                )

                log.info(
                    f"[VERIFY] Attempting inline recovery of secret from Smartsheet API"
                )
                # SmartsheetWebhookService.get_webhook returns the
                # SmartsheetWebhook model directly when the webhook exists,
                # so we read the secret straight off it. The platform
                # client preserves the same `not_found → None` semantics.
                recovered_webhook = await webhook_svc.get_webhook(int(webhook_id))
                recovered = (
                    recovered_webhook.shared_secret
                    if recovered_webhook
                    else None
                )

                if recovered:
                    # Save to database for future use
                    self._webhook_repo.update_secret(worksheet_id, recovered)
                    webhook_secret = recovered
                    log.info(f"[VERIFY] Inline recovery successful for worksheet {worksheet_id}")

            except Exception as e:
                log.warning(f"[VERIFY] Inline recovery failed: {e}")

        # No secret found after all attempts
        if not webhook_secret:
            log.warning(f"[VERIFY] No secret found for worksheet {worksheet_id}")
            return SignatureVerificationResult(
                ok=False,
                status="no_secret",
                message=f"No secret found for worksheet {worksheet_id}"
            )

        # Calculate expected signature
        expected = hmac.new(
            webhook_secret.encode(),
            payload_body,
            hashlib.sha256,
        ).hexdigest()

        # Smartsheet has a bug where it sometimes sends 63-char signatures
        if len(signature) == 63 and signature[0] != '0':
            signature = '0' + signature
            log.debug("[VERIFY] Normalized 63-char signature to 64 chars")

        # Timing-safe comparison
        if hmac.compare_digest(expected, signature):
            return SignatureVerificationResult(ok=True, status="valid")
        else:
            log.warning(f"[VERIFY] Signature mismatch for worksheet {worksheet_id}")
            return SignatureVerificationResult(
                ok=False,
                status="invalid",
                message="Signature does not match"
            )

    async def handle_webhook(
        self,
        payload: WebhookPayload,
    ) -> WebhookResult:
        """
        Handle a webhook payload.

        Args:
            payload: Parsed webhook payload

        Returns:
            WebhookResult with processing outcomes
        """
        flow = FlowLogger(
            log, "WEBHOOK:PROCESS",
            sheet=payload.worksheet_id,
            events=len(payload.events),
        )
        flow.start("Processing webhook payload")

        start_time = time.time()
        result = WebhookResult(
            webhook_id=payload.webhook_id,
            worksheet_id=payload.worksheet_id,
            events_received=len(payload.events),
        )

        try:
            # Get new attachment events
            attachment_events = payload.new_attachment_events
            flow.debug(f"Found {len(attachment_events)} attachment creation events")

            if not attachment_events:
                result.status = "no_attachment_events"
                if payload.events:
                    preview = [
                        (
                            e.object_type.value if hasattr(e.object_type, "value") else str(e.object_type),
                            e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type),
                        )
                        for e in payload.events[:20]
                    ]
                    log.info(
                        "[WEBHOOK:PROCESS] sheet=%s no attachment-creation events after filter "
                        "(received %s events, objectType/eventType preview=%s)",
                        payload.worksheet_id,
                        len(payload.events),
                        preview,
                    )
                flow.success("No attachment events to process")
                return result

            # Filter out self-generated events and already-completed attachments
            external_events = []
            skipped_completed = 0
            skipped_old = 0
            cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=self._max_event_age_minutes)

            # SH-99: batch the "already completed" lookups against Redis in
            # parallel so the filter loop stays single-pass and we only pay
            # one round-trip per event regardless of cluster size.
            completion_checks = await asyncio.gather(*[
                self._dedup.is_attachment_completed(e.object_id)
                for e in attachment_events
            ])
            completed_ids = {
                e.object_id for e, was_done in zip(attachment_events, completion_checks)
                if was_done
            }

            for e in attachment_events:
                if self._is_self_generated(e):
                    continue
                if e.object_id in completed_ids:
                    skipped_completed += 1
                    continue
                # Skip old events (backlog from Smartsheet)
                if e.timestamp and e.timestamp < cutoff_time:
                    skipped_old += 1
                    continue
                external_events.append(e)

            if skipped_completed > 0:
                flow.debug(f"Skipped {skipped_completed} already-completed attachments")
            if skipped_old > 0:
                flow.debug(f"Skipped {skipped_old} old events (older than {self._max_event_age_minutes} min)")

            if not external_events:
                result.status = "all_self_generated"
                result.events_skipped = len(attachment_events)
                flow.success("All events were self-generated, skipped")
                return result

            # Get configs for this worksheet
            flow.step(1, "Fetching active configurations")
            configs = self._config_repo.get_active_webhook_configs(payload.worksheet_id, modes=self._settings.webhook_processing_modes_list)
            if not configs:
                result.status = "no_active_configs"
                flow.warning(f"No active configs for worksheet {payload.worksheet_id}")
                return result

            flow.debug(f"Found {len(configs)} active config(s)")

            # SH-110: Pre-resolve parent_id for attachment.created events.
            # Smartsheet does NOT populate `rowId` on attachment.created events,
            # so without this resolution every batched-multi-attachment webhook
            # would fall into the row_id==0 "unknown row" path and process each
            # event independently — losing the chance to group events that
            # actually belong to the same row. Round-robin across the unique
            # organizations of the configs so no single org bears all the
            # rate-limit cost.
            await self._resolve_attachment_parent_rows(
                external_events, configs, payload.worksheet_id
            )

            # Group events by row to avoid processing the same row multiple times
            flow.step(2, "Grouping events by row")
            row_events: Dict[int, List[WebhookEvent]] = {}
            for event in external_events:
                row_id = event.row_id or 0
                if row_id not in row_events:
                    row_events[row_id] = []
                row_events[row_id].append(event)

            unique_rows = len([r for r in row_events.keys() if r != 0])
            flow.debug(f"Grouped into {unique_rows} unique rows + {len(row_events.get(0, []))} unknown")

            # Process rows with concurrency limit (not individual attachments)
            flow.step(3, f"Processing {len(row_events)} row groups in parallel")
            tasks = []
            for row_id, events in row_events.items():
                task = self._process_row_events_with_semaphore(row_id, events, configs)
                tasks.append(task)

            row_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Count results
            for r in row_results:
                if isinstance(r, Exception):
                    result.events_failed += 1
                    from src.utils.safe_log import safe_exception_message
                    log.error(f"{FlowTags.ERROR} Row processing failed: {safe_exception_message(r)}")
                elif isinstance(r, dict):
                    result.events_processed += r.get("processed", 0)
                    result.attachments_processed += r.get("attachments", 0)
                    result.events_skipped += r.get("skipped", 0)
                    result.events_failed += r.get("failed", 0)

            result.processing_time_ms = int((time.time() - start_time) * 1000)
            result.status = "ok"

            # Record event receipt
            self._webhook_repo.record_event(payload.worksheet_id)

            flow.success(
                "Webhook processed",
                processed=result.events_processed,
                skipped=result.events_skipped,
                failed=result.events_failed,
            )

            return result

        except Exception as e:
            flow.failure("Webhook handling failed", e)
            result.success = False
            result.status = "error"
            result.message = str(e)
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            return result

    async def _process_row_events_with_semaphore(
        self,
        row_id: int,
        events: List[WebhookEvent],
        configs: list,
    ) -> Dict[str, int]:
        """Process all events for a single row with concurrency limiting."""
        async with self._semaphore:
            return await self._process_row_events(row_id, events, configs)

    async def _process_row_events(
        self,
        row_id: int,
        events: List[WebhookEvent],
        configs: list,
    ) -> Dict[str, int]:
        """
        Process all attachment events for a single row.

        This is more efficient than processing each attachment separately
        because we only fetch the row once and process all attachments together.
        """
        result = {"processed": 0, "attachments": 0, "skipped": 0, "failed": 0}

        # If row_id is unknown (0), process events in parallel
        if row_id == 0:
            # Process all events concurrently with semaphore limiting
            tasks = [
                self._process_event_with_semaphore(event, configs)
                for event in events
            ]
            event_results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, res in enumerate(event_results):
                if isinstance(res, Exception):
                    from src.utils.safe_log import safe_exception_message
                    log.error(f"{FlowTags.ERROR} Event processing failed: {safe_exception_message(res)}")
                    result["failed"] += 1
                elif res is None:
                    result["failed"] += 1  # Failed and stored in DLQ
                elif res:
                    result["processed"] += 1
                    result["attachments"] += 1
                else:
                    result["skipped"] += 1
            return result

        # SH-110: removed row_cache check + claim. Other dedup mechanisms
        # cover the cases this guarded against (see __init__ for rationale).

        # Process the row once for all attachments
        for config in configs:
            with log_context(
                org_id=config.organization_id,
                worksheet_id=config.worksheet_id,
                config_id=config.id,
            ):
                flow = FlowLogger(
                    log, "PROCESS:ROW",
                    row=row_id,
                    attachments=len(events),
                )

                try:
                    flow.start(f"Processing row with {len(events)} attachment events")

                    # SH-102: sheet_service is on the smarthub_core async
                    # SmartsheetClient. Token resolution lives in Redis,
                    # rate limiting is cluster-wide — no need for the
                    # threadpool / token-lookup semaphore.
                    sheet_service = SheetService(
                        get_smartsheet_client(
                            config.organization_id, config.runner
                        )
                    )

                    # Fetch row with attachments (single API call for all events)
                    flow.step(1, f"Fetching row {row_id}")
                    row = await sheet_service.get_row(
                        config.worksheet_id,
                        row_id,
                        include_attachments=True,
                    )

                    # SH-109 (conditional): limit processing to the attachments
                    # that fired this webhook batch — only for default mode.
                    # Replace and name_map need to see ALL attachments on the
                    # row so replace can maintain "newest N in N cols" and
                    # name_map can route multiple matching filenames to multiple
                    # columns simultaneously. Unknown / None modes do not scope
                    # (explicit opt-in: safer to pass all attachments than to
                    # drop some by accident on a new mode).
                    cfg_type_raw = (
                        getattr(config.config_type, "value", str(config.config_type))
                        if config.config_type else ""
                    )
                    if cfg_type_raw.lower() == "default":
                        trigger_ids = {e.object_id for e in events if e.object_id}
                        # SH-191: include same-batch form siblings before SH-109
                        trigger_ids = self._expand_triggers_with_coarrived(
                            row, trigger_ids
                        )
                        row = self._scope_row_to_trigger_attachments(row, trigger_ids)

                    # Process the row (SH-101: processor is async-first)
                    flow.step(2, "Processing row attachments")
                    process_result = await self.processor.process_row(config, row)

                    # SH-110: removed row_cache update.

                    # SH-99: mark attachments as completed only when at least one
                    # image was actually processed. Do not mark on bolt-cap deny
                    # or other no-op paths — that would block Smartsheet retries.
                    # SH-191: also mark successfully processed co-arrived siblings
                    # that had no webhook event of their own (Redis, not in-memory).
                    if process_result.images_processed > 0:
                        ids_to_mark = {
                            e.object_id for e in events if e.object_id
                        }
                        for r in (process_result.image_results or []):
                            if r.success and r.attachment_id:
                                ids_to_mark.add(r.attachment_id)
                        await asyncio.gather(*[
                            self._dedup.mark_attachment_completed(aid)
                            for aid in ids_to_mark
                        ])

                    if process_result.images_processed > 0:
                        result["processed"] += 1
                        result["attachments"] += process_result.images_processed
                        flow.success(
                            "Images processed",
                            images=process_result.images_processed,
                        )

                    if process_result.images_failed > 0:
                        result["failed"] += process_result.images_failed
                        log.warning(
                            f"[PROCESS:ROW] Image processing failed for {process_result.images_failed} image(s) in row {row_id}"
                        )
                        self._dlq_repo.store_failed_event(
                            organization_id=config.organization_id,
                            worksheet_id=config.worksheet_id,
                            event_type="attachment_created",
                            event_data={
                                "row_id": row_id,
                                "attachment_ids": [e.object_id for e in events],
                                "failed_count": process_result.images_failed,
                            },
                            error=Exception(f"Image processing failed for {process_result.images_failed} image(s)"),
                            config_id=config.id,
                        )

                    if process_result.images_processed == 0 and process_result.images_failed == 0:
                        result["skipped"] += len(events)
                        flow.success("No new images to process")

                except Exception as e:
                    flow.failure("Failed to process row", e)
                    result["failed"] += len(events)

                    # Store first event in DLQ
                    if events:
                        self._dlq_repo.store_failed_event(
                            organization_id=config.organization_id,
                            worksheet_id=config.worksheet_id,
                            event_type="attachment_created",
                            event_data={
                                "row_id": row_id,
                                "attachment_ids": [e.object_id for e in events],
                            },
                            error=e,
                            config_id=config.id,
                        )

        # SH-99: cleanup is owned by the dedup service (Redis TTL or its
        # internal in-memory fallback). No-op here.

        return result

    async def _process_event_with_semaphore(
        self,
        event: WebhookEvent,
        configs: list,
    ) -> bool:
        """Process an event with concurrency limiting."""
        async with self._semaphore:
            return await self._process_event(event, configs)

    async def _process_event(
        self,
        event: WebhookEvent,
        configs: list,
    ):
        """
        Process a single webhook event (fallback for events without row_id).

        Args:
            event: Webhook event
            configs: Active configurations

        Returns:
            True if processed, False if genuinely skipped (duplicate), None if failed and stored in DLQ
        """
        # SH-99: atomic check-and-claim via Redis SET NX EX (replaces the
        # legacy in-memory dict's TOCTOU check-then-write). If another
        # worker already owns this (attachment_id, row_id) slot — or
        # already processed it within the TTL — we skip.
        if not await self._dedup.try_acquire_processing(
            event.object_id, event.row_id
        ):
            log.debug(
                f"[WEBHOOK:DEDUP] Skipping duplicate event: "
                f"attachment={event.object_id} row={event.row_id}"
            )
            return False

        # Process with each config
        any_processed = False
        had_dlq_error = False
        attachment_id = event.object_id

        for config in configs:
            with log_context(
                org_id=config.organization_id,
                worksheet_id=config.worksheet_id,
                config_id=config.id,
            ):
                flow = FlowLogger(
                    log, "PROCESS:ATTACHMENT",
                    attachment=attachment_id,
                )

                try:
                    flow.start("Processing attachment event")

                    # SH-100: both AttachmentService and SheetService are now
                    # async wrappers backed by smarthub_core's SmartsheetClient.
                    client = get_smartsheet_client(
                        config.organization_id, config.runner
                    )
                    attachment_service = AttachmentService(client)
                    sheet_service = SheetService(client)

                    # Get attachment metadata to find parent row
                    flow.step(1, "Fetching attachment metadata")
                    att_meta = await attachment_service.get_attachment_metadata(
                        config.worksheet_id, attachment_id,
                    )

                    parent_id = att_meta.get("parentId")
                    parent_type = (att_meta.get("parentType") or "").lower()

                    if not parent_id or parent_type != "row":
                        flow.debug(f"Skipping: parent type is '{parent_type}'")
                        continue

                    # Check attachment creation time - skip old attachments
                    created_at_str = att_meta.get("createdAt")
                    if created_at_str:
                        try:
                            # Parse ISO timestamp (Smartsheet format: 2026-02-08T07:16:50Z)
                            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                            cutoff = datetime.now(timezone.utc) - timedelta(minutes=self._max_event_age_minutes)
                            if created_at < cutoff:
                                flow.debug(f"Skipping old attachment (created {created_at_str})")
                                await self._dedup.mark_attachment_completed(attachment_id)
                                continue
                        except (ValueError, TypeError):
                            pass  # Can't parse, proceed with processing

                    row_id = parent_id

                    # SH-110: removed row_cache check + claim + update.

                    # Fetch that specific row (SH-100: direct await on async wrapper)
                    flow.step(2, f"Fetching row {row_id}")
                    row = await sheet_service.get_row(
                        config.worksheet_id,
                        row_id,
                        include_attachments=True,
                    )

                    # SH-109 (conditional): scope to the trigger attachment only
                    # for default mode. Replace and name_map need to see ALL
                    # attachments on the row so replace can maintain the
                    # "newest N in N cols" invariant and name_map can match
                    # multiple filenames to multiple columns simultaneously.
                    # Unknown / None modes do not scope (explicit opt-in).
                    cfg_type_raw = (
                        getattr(config.config_type, "value", str(config.config_type))
                        if config.config_type else ""
                    )
                    if cfg_type_raw.lower() == "default":
                        # SH-191: include same-batch form siblings before SH-109
                        trigger_ids = self._expand_triggers_with_coarrived(
                            row, {attachment_id}
                        )
                        row = self._scope_row_to_trigger_attachments(row, trigger_ids)

                    # Process the row (SH-101: processor is async-first)
                    flow.step(3, "Processing row")
                    result = await self.processor.process_row(config, row)

                    # SH-99: cross-process mark via Redis — only after real work.
                    # SH-191: also mark successfully processed co-arrived siblings.
                    if result.images_processed > 0:
                        ids_to_mark = {attachment_id}
                        for r in (result.image_results or []):
                            if r.success and r.attachment_id:
                                ids_to_mark.add(r.attachment_id)
                        await asyncio.gather(*[
                            self._dedup.mark_attachment_completed(aid)
                            for aid in ids_to_mark
                        ])

                    if result.images_processed > 0:
                        any_processed = True
                        flow.success(
                            "Images processed",
                            images=result.images_processed,
                            row_id=row_id,
                        )

                    if result.images_failed > 0:
                        had_dlq_error = True
                        log.warning(
                            f"[PROCESS:ATTACHMENT] Image processing failed for {result.images_failed} image(s) in row {row_id}"
                        )
                        self._dlq_repo.store_failed_event(
                            organization_id=config.organization_id,
                            worksheet_id=config.worksheet_id,
                            event_type="attachment_created",
                            event_data={
                                "attachment_id": attachment_id,
                                "row_id": row_id,
                                "failed_count": result.images_failed,
                            },
                            error=Exception(f"Image processing failed for {result.images_failed} image(s)"),
                            config_id=config.id,
                        )

                    if result.images_processed == 0 and result.images_failed == 0:
                        flow.success("No new images to process")

                except Exception as e:
                    flow.failure("Failed to process attachment", e)
                    had_dlq_error = True

                    # Store in Dead Letter Queue for retry
                    self._dlq_repo.store_failed_event(
                        organization_id=config.organization_id,
                        worksheet_id=config.worksheet_id,
                        event_type="attachment_created",
                        event_data={
                            "attachment_id": attachment_id,
                            "row_id": event.row_id,
                            "event": event.__dict__ if hasattr(event, '__dict__') else str(event),
                        },
                        error=e,
                        config_id=config.id,
                    )

        if had_dlq_error and not any_processed:
            return None  # Failed and stored in DLQ — distinct from a genuine skip
        return any_processed

    def _is_self_generated(self, event: WebhookEvent) -> bool:
        """Check if event was generated by our own changes."""
        if not event.change_agent:
            return False
        return CHANGE_AGENT_ID in event.change_agent


# Singleton instance
_webhook_handler: Optional[WebhookHandler] = None


def get_webhook_handler() -> WebhookHandler:
    """Get the global webhook handler singleton."""
    global _webhook_handler
    if _webhook_handler is None:
        _webhook_handler = WebhookHandler()
    return _webhook_handler
