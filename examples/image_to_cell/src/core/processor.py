"""
Image Processor
===============

Main entry point for image processing.

SH-101 — the orchestration layer is now async-first. ``process_config``,
``backfill_worksheet``, ``backfill_single_config``, ``process_row``, and
the parallel row-processing helpers are all ``async def``. The SH-100
``asyncio.run`` bridges around ``SheetService`` are gone — wrappers are
awaited directly. Per-config row work uses ``asyncio.gather`` over async
tasks (capped by a semaphore) instead of a ``ThreadPoolExecutor`` over
sync callables.
"""

import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

from smarthub_core.platforms.smartsheet import SmartsheetClient

from src.models.config import ImageToCellConfig
from src.models.row_state import RowState
from src.models.results import ProcessingResult, RowResult, BackfillResult
from src.services.smartsheet.client import get_smartsheet_client
from src.services.smartsheet.sheets import SheetService
from src.services.supabase.client import SupabaseService, get_supabase_service
from src.services.supabase.config_repo import ConfigRepository
from src.services.supabase.state_repo import StateRepository
from src.core.row_processor import RowProcessor
from src.config.settings import Settings, get_settings
from src.services.backfill_lock import (
    BACKFILL_ALREADY_RUNNING,
    BackfillLease,
    hold_backfill_lease,
    hold_worksheet_backfill_lock,
)
from src.services.bolt_cap import invalidate_usage_cache
from src.services.i2c_bolt_gate import (
    PriorLogUnavailable,
    _fetch_prior_processed,
    preflight_backfill_budget,
    preflight_pause_if_blocked,
)


log = logging.getLogger(__name__)


class ImageProcessor:
    """
    Main image processing facade.

    This is the primary entry point for all image processing operations.
    It coordinates between services and processors.
    """

    def __init__(
        self,
        supabase: Optional[SupabaseService] = None,
        settings: Optional[Settings] = None,
    ):
        """
        Initialize image processor.

        Args:
            supabase: Supabase service (uses singleton if not provided)
            settings: Application settings (uses singleton if not provided)
        """
        self._db = supabase or get_supabase_service()
        self._settings = settings or get_settings()
        self._config_repo = ConfigRepository(self._db)
        self._state_repo = StateRepository(self._db)

        # SH-102: the legacy `_ss_cache` of `SmartsheetService` instances
        # is gone. RowProcessor no longer needs a sync SmartsheetService
        # reference, and SheetService gets a fresh `SmartsheetClient`
        # per org via `_get_sheet_service`. Token cache and rate-limit
        # state live in Redis under stable per-(org, slug) keys, so the
        # in-process cache became dead weight.

    async def process_config(
        self,
        config: ImageToCellConfig,
        max_rows: Optional[int] = None,
    ) -> ProcessingResult:
        """
        Process a complete configuration.

        Fetches the sheet and processes all rows with attachments.

        Args:
            config: Configuration to process
            max_rows: Optional limit on rows to process

        Returns:
            ProcessingResult with outcomes
        """
        start_time = time.time()
        result = ProcessingResult(
            success=True,
            config_id=config.id,
            worksheet_id=config.worksheet_id,
            started_at=time.time(),
        )

        try:
            block_reason = preflight_pause_if_blocked(config.organization_id, config.id)
            if block_reason:
                log.info(
                    "Skipping process_config (bolt preflight): %s config=%s",
                    block_reason,
                    config.id,
                )
                result.completed_at = time.time()
                result.processing_time_ms = int((time.time() - start_time) * 1000)
                return result

            # Get Smartsheet service for this organization. Legacy ss is still
            # needed for RowProcessor (migrates in SH-100/AttachmentService +
            # SH-101); SheetService now uses the async SmartsheetClient.
            # SH-102: RowProcessor accepts None for the legacy `ss`
            # parameter; production paths no longer build it.
            ss = None
            # SH-178: background work runs as the config's runner
            # (runner_user_id, falling back to created_by).
            sheet_service = self._get_sheet_service(
                config.organization_id, config.runner
            )
            row_processor = RowProcessor(
                ss,
                self._state_repo,
                self._settings,
                organization_id=config.organization_id,
                owner_user_id=config.runner,
            )

            # SH-101: SheetService is now awaited directly (no bridge).
            log.info(f"Fetching sheet {config.worksheet_id} for config {config.configuration_name}")
            sheet = await sheet_service.get_sheet(
                config.worksheet_id,
                max_pages=self._settings.max_sheet_pages,
            )

            # Get rows with attachments (extract_row_attachments is sync — pure)
            rows = sheet.get("rows", [])
            rows_with_attachments = [
                row for row in rows
                if sheet_service.extract_row_attachments(row, images_only=True)
            ]

            log.info(f"Found {len(rows_with_attachments)} rows with attachments")

            if max_rows:
                rows_with_attachments = rows_with_attachments[:max_rows]

            # Batch get states
            row_ids = [str(row["id"]) for row in rows_with_attachments]
            states = self._state_repo.get_batch(config.id, [int(rid) for rid in row_ids])

            # Process rows (SH-101: async-first)
            await self._process_rows_parallel(
                config,
                rows_with_attachments,
                states,
                row_processor,
                result,
            )

            # Update config last_run_at
            if result.rows_processed > 0:
                self._config_repo.update_last_run(config.id)

            result.success = result.rows_failed == 0
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)

            log.info(
                f"Config {config.configuration_name}: processed {result.rows_processed} rows, "
                f"skipped {result.rows_skipped}, failed {result.rows_failed} "
                f"({result.processing_time_ms}ms)"
            )

            return result

        except Exception as e:
            log.error(f"Failed to process config {config.id}: {e}")
            result.success = False
            result.error = str(e)
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            return result

    async def process_row(
        self,
        config: ImageToCellConfig,
        row: Dict[str, Any],
        state: Optional[RowState] = None,
        skip_usage_logging: bool = False,
    ) -> RowResult:
        """
        Process a single row.

        Args:
            config: Configuration
            row: Row data from Smartsheet
            state: Optional existing state
            skip_usage_logging: If True, skip usage logging (used by DLQ
                retries to avoid double-counting)

        Returns:
            RowResult with outcome
        """
        row_id = row["id"]
        block_reason = preflight_pause_if_blocked(config.organization_id, config.id)
        if block_reason:
            log.info(
                "Skipping process_row (bolt preflight): %s config=%s row=%s",
                block_reason,
                config.id,
                row_id,
            )
            return RowResult(
                success=True,
                row_id=row_id,
                images_skipped=0,
                processing_time_ms=0,
            )

        # SH-102: ss = None — RowProcessor's legacy slot is unused.
        # SH-179: must forward the owner identity so OAuth-only orgs resolve
        # the per-user broker token. Without owner_user_id the broker falls
        # back to the org PAT; orgs with no PAT fail get_attachment with
        # "No Smartsheet credentials" even when Connect is green.
        # SH-178: that identity is the config's runner (created_by fallback).
        row_processor = RowProcessor(
            None,
            self._state_repo,
            self._settings,
            organization_id=config.organization_id,
            owner_user_id=config.runner,
        )
        # process_row is the live-event entry point (webhook handler + DLQ
        # retries of the original webhook event), so it is not a scan.
        return await row_processor.process(
            config,
            row,
            state,
            skip_usage_logging=skip_usage_logging,
            is_scan_context=False,
        )

    async def backfill_worksheet(
        self,
        worksheet_id: int,
        max_pages: Optional[int] = None,
    ) -> BackfillResult:
        """
        Run initial backfill for a worksheet.

        Processes all existing attachments that were uploaded
        before the configuration was created.

        Args:
            worksheet_id: Worksheet to backfill
            max_pages: Optional page limit

        Returns:
            BackfillResult with outcomes
        """
        start_time = time.time()
        result = BackfillResult(
            worksheet_id=worksheet_id,
            started_at=time.time(),
        )
        # The org whose usage cache we will invalidate at the end of the
        # backfill (B18). Captured from the first config we process so the
        # finally block can run even if an exception fires before we reach
        # the per-config loop.
        invalidate_org_id: Optional[str] = None

        try:
            # Get active configs for this worksheet
            configs = self._config_repo.get_active_webhook_configs(worksheet_id)

            if not configs:
                log.info(f"No active webhook configs for worksheet {worksheet_id}")
                return result

            log.info(f"Starting backfill for worksheet {worksheet_id} ({len(configs)} configs)")

            async with hold_worksheet_backfill_lock(
                worksheet_id,
                ttl_s=self._settings.backfill_lock_ttl_s,
                heartbeat_interval_s=self._settings.backfill_lock_heartbeat_s,
            ) as lock_acquired:
                if not lock_acquired:
                    log.info(
                        "Skipping backfill (lock busy) worksheet_id=%s",
                        worksheet_id,
                    )
                    result.success = False
                    result.error = BACKFILL_ALREADY_RUNNING
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                # Use the first config's org for Smartsheet access
                # (all configs for same worksheet should have same org)
                config = configs[0]
                invalidate_org_id = config.organization_id
                block_reason = preflight_pause_if_blocked(
                    config.organization_id, config.id
                )
                if block_reason:
                    log.info(
                        "Skipping backfill (bolt preflight): %s worksheet=%s",
                        block_reason,
                        worksheet_id,
                    )
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                # SH-102: RowProcessor accepts None for the legacy `ss`
                # parameter; production paths no longer build it.
                ss = None
                sheet_service = self._get_sheet_service(
                    config.organization_id, config.runner
                )

                # SH-101: SheetService awaited directly. Fetch the sheet and
                # the per-row attachment list in parallel — they hit different
                # endpoints and the responses are independent.
                max_pages = max_pages or self._settings.backfill_max_pages
                sheet, rows_with_attachments = await asyncio.gather(
                    sheet_service.get_sheet(worksheet_id, max_pages=max_pages),
                    sheet_service.get_rows_with_attachments(worksheet_id, max_pages),
                )
                result.sheet_fetched = True
                result.total_rows_in_sheet = len(sheet.get("rows", []))
                result.rows_with_attachments = len(rows_with_attachments)

                if not rows_with_attachments:
                    log.info(f"No rows with attachments in worksheet {worksheet_id}")
                    return result

                # Process each config
                for config in configs:
                    # Budget preflight: reject if this backfill would exceed the org's
                    # ceiling. Closes the race where parallel backfill workers each
                    # read a stale consumed-bolts value and collectively overshoot.
                    budget_issue = preflight_backfill_budget(
                        config.organization_id,
                        config,
                        rows_with_attachments,
                        sheet_service,
                    )
                    if budget_issue:
                        log.warning(
                            "Skipping backfill for config %s: %s "
                            "(needed=%s available=%s bolts_per_image=%s)",
                            config.id,
                            budget_issue["reason"],
                            budget_issue["images_needed"],
                            budget_issue["images_available"],
                            budget_issue["bolts_per_image"],
                        )
                        skipped = BackfillResult(worksheet_id=config.worksheet_id)
                        skipped.rows_skipped = len(rows_with_attachments)
                        skipped.success = False
                        skipped.error = (
                            f"insufficient_bolts_for_backfill: needed "
                            f"~{budget_issue['images_needed']} images, only "
                            f"~{budget_issue['images_available']} available"
                        )
                        result.merge(skipped)
                        continue

                    config_result = await self._backfill_config(
                        config, rows_with_attachments, ss, sheet_service
                    )
                    result.merge(config_result)

                result.completed_at = time.time()
                result.processing_time_ms = int((time.time() - start_time) * 1000)

                log.info(
                    f"Backfill for worksheet {worksheet_id}: "
                    f"processed {result.rows_processed}, skipped {result.rows_skipped}, "
                    f"failed {result.rows_failed} ({result.processing_time_ms}ms)"
                )

                return result

        except Exception as e:
            log.error(f"Backfill failed for worksheet {worksheet_id}: {e}")
            result.success = False
            result.error = str(e)
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            return result

        finally:
            # B18: drop the cached subscription / consumed-bolts snapshot so
            # the next config in the worksheet (and the next webhook event
            # for this org) sees the consumption from THIS backfill instead
            # of a 30-second-stale snapshot taken before we started.
            if invalidate_org_id is not None:
                invalidate_usage_cache(invalidate_org_id)

    async def backfill_single_config(
        self,
        config: ImageToCellConfig,
        max_pages: Optional[int] = None,
    ) -> BackfillResult:
        """Run initial backfill for exactly one config (create-time path).

        Unlike :meth:`backfill_worksheet`, this never loads or processes sibling
        configs on the same worksheet. Unlike :meth:`backfill_config_prefetched`
        (SH-183 interactive Run Backfill), this path:

        - acquires the worksheet lease itself (background, not HTTP 202),
        - resolves Smartsheet access as the config **runner** / created_by,
        - does **not** re-arm exhausted ``failed_attempts`` (first-time fill).

        Skip / column / SH-108 / SH-145 semantics match :meth:`_backfill_config`.
        """
        start_time = time.time()
        worksheet_id = config.worksheet_id
        result = BackfillResult(
            worksheet_id=worksheet_id,
            started_at=time.time(),
        )

        try:
            log.info(
                "Starting single-config backfill config=%s worksheet=%s",
                config.id,
                worksheet_id,
            )

            async with hold_worksheet_backfill_lock(
                worksheet_id,
                ttl_s=self._settings.backfill_lock_ttl_s,
                heartbeat_interval_s=self._settings.backfill_lock_heartbeat_s,
            ) as lock_acquired:
                if not lock_acquired:
                    log.info(
                        "Skipping single-config backfill (lock busy) "
                        "config=%s worksheet_id=%s",
                        config.id,
                        worksheet_id,
                    )
                    result.success = False
                    result.error = BACKFILL_ALREADY_RUNNING
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                block_reason = preflight_pause_if_blocked(
                    config.organization_id, config.id
                )
                if block_reason:
                    log.info(
                        "Skipping single-config backfill (bolt preflight): %s "
                        "config=%s worksheet=%s",
                        block_reason,
                        config.id,
                        worksheet_id,
                    )
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                sheet_service = self._get_sheet_service(
                    config.organization_id, config.runner
                )
                max_pages = max_pages or self._settings.backfill_max_pages
                sheet, rows_with_attachments = await asyncio.gather(
                    sheet_service.get_sheet(worksheet_id, max_pages=max_pages),
                    sheet_service.get_rows_with_attachments(worksheet_id, max_pages),
                )
                result.sheet_fetched = True
                result.total_rows_in_sheet = len(sheet.get("rows", []))
                result.rows_with_attachments = len(rows_with_attachments)

                if not rows_with_attachments:
                    log.info(
                        "No rows with attachments for single-config backfill "
                        "config=%s worksheet=%s",
                        config.id,
                        worksheet_id,
                    )
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                budget_issue = preflight_backfill_budget(
                    config.organization_id,
                    config,
                    rows_with_attachments,
                    sheet_service,
                )
                if budget_issue:
                    log.warning(
                        "Skipping single-config backfill for config %s: %s "
                        "(needed=%s available=%s bolts_per_image=%s)",
                        config.id,
                        budget_issue["reason"],
                        budget_issue["images_needed"],
                        budget_issue["images_available"],
                        budget_issue["bolts_per_image"],
                    )
                    result.rows_skipped = len(rows_with_attachments)
                    result.success = False
                    result.error = (
                        f"insufficient_bolts_for_backfill: needed "
                        f"~{budget_issue['images_needed']} images, only "
                        f"~{budget_issue['images_available']} available"
                    )
                    result.completed_at = time.time()
                    result.processing_time_ms = int((time.time() - start_time) * 1000)
                    return result

                config_result = await self._backfill_config(
                    config, rows_with_attachments, None, sheet_service
                )
                result.merge(config_result)
                result.completed_at = time.time()
                result.processing_time_ms = int((time.time() - start_time) * 1000)

                log.info(
                    "Single-config backfill config=%s worksheet=%s: "
                    "processed %s, skipped %s, failed %s (%sms)",
                    config.id,
                    worksheet_id,
                    result.rows_processed,
                    result.rows_skipped,
                    result.rows_failed,
                    result.processing_time_ms,
                )
                return result

        except Exception as e:
            log.error(
                "Single-config backfill failed config=%s worksheet=%s: %s",
                config.id,
                worksheet_id,
                e,
            )
            result.success = False
            result.error = str(e)
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            return result

        finally:
            invalidate_usage_cache(config.organization_id)

    async def backfill_config_prefetched(
        self,
        config: ImageToCellConfig,
        rows_with_attachments: List[Dict[str, Any]],
        sheet_service: SheetService,
        *,
        lease: Optional[BackfillLease] = None,
        session_user_id: Optional[str] = None,
    ) -> BackfillResult:
        """Run a backfill for exactly one config (SH-183 manual Run Backfill).

        Unlike :meth:`backfill_worksheet`, this never touches sibling configs
        on the same worksheet. The HTTP route has already:

        - validated auth/tenancy and that the config is runnable,
        - fetched the sheet rows as the **session user** (interactive flow),
        - revalidated the bolt budget, and
        - secured the worksheet backfill lease (``lease``).

        This method holds that same pre-acquired lease for the duration
        (heartbeat + release via :func:`hold_backfill_lease`) — it must not
        re-acquire the lock. Only still-unresolved images are retried:
        synthetic/live cell-state reconciliation is unchanged, and exhausted
        ``failed_attempts`` are re-armed only for targets whose output is
        still missing (see ``rearm_failed_attempts`` in
        :meth:`_backfill_config`).
        """
        start_time = time.time()
        result = BackfillResult(
            worksheet_id=config.worksheet_id,
            started_at=time.time(),
        )
        result.sheet_fetched = True
        result.rows_with_attachments = len(rows_with_attachments)

        try:
            if lease is not None:
                async with hold_backfill_lease(lease):
                    config_result = await self._backfill_config(
                        config,
                        rows_with_attachments,
                        None,
                        sheet_service,
                        rearm_failed_attempts=True,
                        owner_user_id=session_user_id,
                    )
            else:
                config_result = await self._backfill_config(
                    config,
                    rows_with_attachments,
                    None,
                    sheet_service,
                    rearm_failed_attempts=True,
                    owner_user_id=session_user_id,
                )
            result.merge(config_result)
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            log.info(
                "Config-scoped backfill config=%s worksheet=%s: processed %s, "
                "skipped %s, failed %s (%sms)",
                config.id,
                config.worksheet_id,
                result.rows_processed,
                result.rows_skipped,
                result.rows_failed,
                result.processing_time_ms,
            )
            return result
        except Exception as e:
            log.error(
                "Config-scoped backfill failed config=%s: %s", config.id, e
            )
            result.success = False
            result.error = str(e)
            result.completed_at = time.time()
            result.processing_time_ms = int((time.time() - start_time) * 1000)
            return result
        finally:
            # Same as backfill_worksheet: drop the cached usage snapshot so
            # the next webhook event / backfill sees this run's consumption.
            invalidate_usage_cache(config.organization_id)

    def _rearm_failed_attempts(
        self,
        states: Dict[int, RowState],
    ) -> int:
        """SH-183: re-arm exhausted ``failed_attempts`` for unresolved targets.

        A manual Run Backfill is an explicit user request to retry images
        that never rendered, including ones that exhausted their bounded
        retry budget. An entry in ``failed_attempts`` whose signature is NOT
        in ``last_ids`` means the upload never succeeded — its output is
        still missing — so the counter is cleared and the attachment gets a
        fresh retry budget. Signatures already in ``last_ids`` (completed
        work) are left untouched, and the SH-105/SH-145 cell-tier checks in
        RowProcessor still prevent overwriting manually placed images.

        Mutates ``states`` in place; returns how many entries were re-armed.
        The cleared counters are persisted by RowProcessor's normal state
        save after the row is processed.
        """
        rearmed = 0
        for state in states.values():
            stale = [
                sig for sig in state.failed_attempts if sig not in state.last_ids
            ]
            for sig in stale:
                state.failed_attempts.pop(sig, None)
                rearmed += 1
        if rearmed:
            log.info(
                "SH-183: re-armed %d exhausted failed_attempts entries for "
                "unresolved targets",
                rearmed,
            )
        return rearmed

    def _build_synthetic_states_for_backfill(
        self,
        config: ImageToCellConfig,
        rows_without_state: List[Dict[str, Any]],
        sheet_service: SheetService,
    ) -> Dict[int, RowState]:
        """
        Build synthetic RowState objects for rows that were previously processed
        by any config on the same worksheet, so the backfill correctly skips
        images already placed in target columns.

        Applies to default, name_map, and replace modes:
            - default: state.locked_columns + state.last_ids skip already-filled slots.
            - name_map: state.last_ids + state.last_ts skip already-processed signatures.
            - replace: state.last_ids lets ReplaceMode.filter_attachments skip
              re-uploads of attachments already in the target columns. Replace
              ignores locked_columns at runtime so calling lock_column here is
              a harmless no-op.

        Fail-open: returns {} on any error so the backfill continues unblocked.
        """
        config_type = getattr(
            getattr(config, "config_type", None), "value", "default"
        ).lower()
        if config_type not in ("default", "name map", "replace"):
            return {}

        target_col_ids = [c for c in (getattr(config, "image_columns", None) or []) if c]
        if not target_col_ids or not rows_without_state:
            return {}

        # Reuse the shared retry-aware helper. PriorLogUnavailable propagates
        # so the backfill caller can abort cleanly instead of treating a
        # transient query failure as 'no prior history' and re-billing already
        # processed images.
        row_ids_to_check = [int(r["id"]) for r in rows_without_state]
        prior_by_row = _fetch_prior_processed(
            config.organization_id,
            config.worksheet_id,
            target_col_ids,
            row_ids_to_check,
        )

        if not prior_by_row:
            return {}

        image_cols = list(config.image_columns or [])
        synthetic: Dict[int, RowState] = {}

        for row in rows_without_state:
            row_id = int(row.get("id") or 0)
            prior = prior_by_row.get(row_id)
            if not prior:
                continue

            atts = sheet_service.extract_row_attachments(row, images_only=True)
            att_by_name = {a.name: a for a in atts}

            # SH-108: drop prior-log entries whose target cell is currently
            # empty (shared with compute_backfill_estimate).
            prior = RowProcessor.filter_prior_for_filled_cells(
                prior, row, row_id=row_id
            )

            state = RowState(config_id=config.id, row_id=row_id)
            for col_id, fname in prior:
                att = att_by_name.get(fname)
                if att is None:
                    continue
                state.last_ids.add(att.signature)
                if att.created_timestamp > state.last_ts:
                    state.last_ts = att.created_timestamp
                try:
                    col_idx = image_cols.index(col_id)
                    state.lock_column(col_idx)
                except ValueError:
                    pass

            if state.last_ids:
                synthetic[row_id] = state

        log.info(
            "processor: built synthetic states for %d/%d rows org=%s config=%s",
            len(synthetic),
            len(rows_without_state),
            config.organization_id,
            config.id,
        )
        return synthetic

    async def _backfill_config(
        self,
        config: ImageToCellConfig,
        rows: List[Dict[str, Any]],
        ss: Any = None,
        sheet_service: Optional[SheetService] = None,
        *,
        rearm_failed_attempts: bool = False,
        owner_user_id: Optional[str] = None,
    ) -> BackfillResult:
        """Backfill a single config (SH-101 async).

        ``sheet_service`` is passed in from :meth:`backfill_worksheet` so the
        single async-backed instance is shared across all configs of a
        worksheet. The argument is optional to keep the existing test
        signatures (``_backfill_config(cfg, rows, ss)``) working — when
        omitted we build a fresh one from the org.

        SH-183 additions:

        - ``rearm_failed_attempts`` — manual Run Backfill re-arms exhausted
          retry budgets, but only for attachments whose upload never
          succeeded (signature not in ``last_ids``).
        - ``owner_user_id`` — Smartsheet identity for row processing.
          Defaults to the config's ``created_by`` (background behaviour);
          the manual interactive flow passes the authenticated session user.
        """
        result = BackfillResult(worksheet_id=config.worksheet_id)
        effective_user_id = owner_user_id or config.runner
        row_processor = RowProcessor(
            ss,
            self._state_repo,
            self._settings,
            organization_id=config.organization_id,
            owner_user_id=effective_user_id,
        )
        if sheet_service is None:
            sheet_service = self._get_sheet_service(
                config.organization_id, effective_user_id
            )

        # Batch get existing states for this config_id
        row_ids = [row["id"] for row in rows]
        states = self._state_repo.get_batch(config.id, row_ids)

        if rearm_failed_attempts and states:
            self._rearm_failed_attempts(states)

        # For rows with no existing state, build synthetic states from prior
        # processing history so the backfill skips images already placed by
        # a previous config on the same worksheet.
        #
        # If the prior-log lookup fails after retries we ABORT this config's
        # backfill — running with empty synthetic state would re-bill every
        # already-processed image. The user-facing error is retryable.
        rows_without_state = [r for r in rows if r["id"] not in states]
        if rows_without_state:
            try:
                synthetic = self._build_synthetic_states_for_backfill(
                    config, rows_without_state, sheet_service
                )
                states.update(synthetic)
            except PriorLogUnavailable as e:
                log.error(
                    "Backfill aborted for config %s — prior log unavailable: %s",
                    config.id,
                    e,
                )
                result.success = False
                result.error = (
                    f"prior_log_unavailable: imagetocell_usage_log query "
                    f"failed after retries. Backfill safe to retry once "
                    f"the database is reachable. Detail: {e}"
                )
                result.rows_skipped = len(rows)
                return result

        # SH-101: process rows in parallel via asyncio.gather + semaphore
        # (was ThreadPoolExecutor + submit/as_completed).
        sem = asyncio.Semaphore(self._settings.backfill_max_workers)

        async def _one(row_data):
            async with sem:
                rid = row_data["id"]
                try:
                    row_result = await row_processor.process(
                        config, row_data, states.get(rid), is_scan_context=True
                    )
                    return rid, row_result, None
                except Exception as e:
                    return rid, None, e

        outcomes = await asyncio.gather(*[_one(r) for r in rows])
        for row_id, row_result, err in outcomes:
            if err is not None:
                log.warning(f"Backfill failed for row {row_id}: {err}")
                result.rows_failed += 1
            elif row_result.images_processed > 0:
                result.rows_processed += 1
            else:
                result.rows_skipped += 1

        # Update last_run_at
        if result.rows_processed > 0:
            self._config_repo.update_last_run(config.id)

        return result

    async def _process_rows_parallel(
        self,
        config: ImageToCellConfig,
        rows: List[Dict[str, Any]],
        states: Dict[int, RowState],
        row_processor: RowProcessor,
        result: ProcessingResult,
    ) -> None:
        """Process rows in parallel (SH-101 async)."""
        max_workers = self._settings.max_concurrent_rows
        sem = asyncio.Semaphore(max_workers)

        async def _one(row_data):
            async with sem:
                rid = row_data["id"]
                try:
                    row_result = await row_processor.process(
                        config, row_data, states.get(rid), is_scan_context=True
                    )
                    return rid, row_result, None
                except Exception as e:
                    return rid, None, e

        outcomes = await asyncio.gather(*[_one(r) for r in rows])
        for row_id, row_result, err in outcomes:
            if err is not None:
                log.error(f"Row processing failed for {row_id}: {err}")
                result.rows_failed += 1
            else:
                result.add_row_result(row_result)

    def _get_sheet_service(
        self,
        organization_id: str,
        owner_user_id: Optional[str] = None,
    ) -> SheetService:
        """Build an async :class:`SheetService` for the given org (SH-100).

        ``owner_user_id`` (the config's ``created_by``) lets the token
        resolver prefer that user's per-tool OAuth token on US/EU plans.

        We do NOT cache the underlying ``SmartsheetClient`` instance here:
        the shared state (token cache, rate-limit window, in-flight SDK
        builds) lives in Redis under stable per-(org, slug) keys, so each
        fresh instance picks up the same cache. The class itself is
        cheap to construct.
        """
        return SheetService(get_smartsheet_client(organization_id, owner_user_id))
