"""
Webhook Lifecycle Manager
=========================

Background service that ensures webhook health across all configurations.

Responsibilities:
1. HEALTH MONITORING - Check webhook status every 5 minutes
2. AUTO-RECOVERY - Register missing webhooks, fix broken ones
3. ZOMBIE CLEANUP - Delete orphan/duplicate/stale webhooks
4. KEEP-ALIVE - Prevent webhook expiration
5. URL MISMATCH DETECTION - Update outdated callback URLs
6. DUAL TABLE SYNC - Maintain consistency across tables

This is critical for production reliability.
Based on v2 robustness patterns.
"""

import asyncio
import logging
import os
import tempfile
import httpx
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from src.config.settings import Settings, get_settings
from src.config.constants import TABLE_NAMES
from src.services.supabase.client import get_supabase_service
from src.services.supabase.config_repo import ConfigRepository
from src.services.supabase.webhook_repo import WebhookRepository, _ensure_encrypted_secret
from src.services.smartsheet.client import get_smartsheet_client
from src.services.smartsheet.webhooks import (
    SmartsheetWebhookService,
    SmartsheetWebhook,
    WebhookStatus,
)
from src.utils.logging import (
    FlowLogger,
    log_context,
    log_separator,
    log_summary,
)


log = logging.getLogger(__name__)


# Configuration constants (based on v2)
MIN_AGE_FOR_REREGISTER_MINUTES = 15  # Wait before recreating NEW_NOT_VERIFIED webhooks
HEALTH_CHECK_TIMEOUT_S = 5  # Timeout for FastAPI health check
WORKSHEET_WEBHOOKS_TABLE = "worksheet_webhooks"  # Secondary table for trigger compatibility


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class HealthCheckResult:
    """Result of a health check cycle."""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    organizations_checked: int = 0
    webhooks_checked: int = 0
    webhooks_healthy: int = 0
    webhooks_registered: int = 0
    webhooks_re_enabled: int = 0
    webhooks_url_updated: int = 0
    webhooks_waiting: int = 0  # NEW_NOT_VERIFIED within age threshold
    zombies_deleted: int = 0
    duplicates_deleted: int = 0
    stale_deleted: int = 0
    unowned_skipped: int = 0  # SH-178: visible-but-not-provably-ours, left alone
    errors: int = 0
    skipped_no_fastapi: bool = False

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "organizations_checked": self.organizations_checked,
            "webhooks_checked": self.webhooks_checked,
            "webhooks_healthy": self.webhooks_healthy,
            "webhooks_registered": self.webhooks_registered,
            "webhooks_re_enabled": self.webhooks_re_enabled,
            "webhooks_url_updated": self.webhooks_url_updated,
            "webhooks_waiting": self.webhooks_waiting,
            "zombies_deleted": self.zombies_deleted,
            "duplicates_deleted": self.duplicates_deleted,
            "stale_deleted": self.stale_deleted,
            "unowned_skipped": self.unowned_skipped,
            "errors": self.errors,
            "skipped_no_fastapi": self.skipped_no_fastapi,
        }


# =============================================================================
# Webhook Lifecycle Manager
# =============================================================================

class WebhookLifecycleManager:
    """
    Manages the complete lifecycle of webhooks across all organizations.

    Runs as a background service, periodically checking and maintaining
    webhook health.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        check_interval_seconds: int = 300,  # 5 minutes
    ):
        """
        Initialize lifecycle manager.

        Args:
            settings: Application settings
            check_interval_seconds: How often to run health checks
        """
        self._settings = settings or get_settings()
        self._check_interval = check_interval_seconds
        self._running = False
        self._last_result: Optional[HealthCheckResult] = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5

        # Repositories
        self._config_repo = ConfigRepository()
        self._webhook_repo = WebhookRepository()
        self._db = get_supabase_service()

    @property
    def is_running(self) -> bool:
        """Check if lifecycle manager is running."""
        return self._running

    @property
    def last_result(self) -> Optional[HealthCheckResult]:
        """Get the last health check result."""
        return self._last_result

    async def _is_fastapi_healthy(self) -> bool:
        """
        Check if FastAPI is responding before attempting webhook operations.

        Based on v2 pattern - prevents webhook registration when endpoint is down,
        which would cause immediate verification failure.

        Returns:
            True if FastAPI health endpoint responds, False otherwise
        """
        try:
            port = self._settings.port
            async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_S) as client:
                response = await client.get(f"http://localhost:{port}/health")
                return response.status_code == 200
        except Exception as e:
            log.warning(f"[WEBHOOK:HEALTH] FastAPI health check failed: {e}")
            return False

    async def start(self) -> None:
        """Start the lifecycle manager background loop."""
        if self._running:
            log.warning("[WEBHOOK:LIFECYCLE] Already running")
            return

        self._running = True
        log.info("[WEBHOOK:LIFECYCLE] Starting webhook lifecycle manager")
        log.info(f"[WEBHOOK:LIFECYCLE] Check interval: {self._check_interval}s")

        # Warn early if neither legacy nor gateway callback URL is configured
        if not self._settings.has_webhook_callback_config:
            log.error(
                "[WEBHOOK:LIFECYCLE] WARNING: neither WEBHOOK_CALLBACK_URL nor "
                "SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL is configured! "
                "Webhooks cannot be registered without a valid callback URL."
            )

        lock_path = os.path.join(tempfile.gettempdir(), "i2c_webhook_lifecycle.lock")

        while self._running:
            try:
                from filelock import FileLock, Timeout
                lock = FileLock(lock_path, timeout=0)
                try:
                    with lock:
                        result = await self.run_health_check()
                        self._last_result = result
                        self._consecutive_failures = 0  # Reset on success
                        log_summary(log, "WEBHOOK HEALTH CHECK COMPLETE", result.to_dict())
                except Timeout:
                    log.debug("[WEBHOOK:LIFECYCLE] Another worker is running health check, skipping")

            except Exception as e:
                self._consecutive_failures += 1
                log.error(f"[WEBHOOK:LIFECYCLE] Health check failed: {e}")

                # Circuit breaker: after N consecutive failures, wait much longer before retrying
                if self._consecutive_failures >= self._max_consecutive_failures:
                    extended_sleep = self._check_interval * 3
                    log.error(
                        f"[WEBHOOK:LIFECYCLE] CIRCUIT BREAKER: {self._consecutive_failures} consecutive "
                        f"failures. Backing off for {extended_sleep}s before next attempt."
                    )
                    if self._running:
                        await asyncio.sleep(extended_sleep)
                    self._consecutive_failures = 0
                    continue

            # Sleep until next check
            if self._running:
                log.info(
                    f"[WEBHOOK:LIFECYCLE] Next check in {self._check_interval}s"
                )
                await asyncio.sleep(self._check_interval)

    async def stop(self) -> None:
        """Stop the lifecycle manager."""
        log.info("[WEBHOOK:LIFECYCLE] Stopping...")
        self._running = False

    async def run_health_check(self) -> HealthCheckResult:
        """
        Run a complete health check across all organizations.

        This is the main function that:
        1. CRITICAL: Verify FastAPI is healthy before any operations
        2. Gets all active webhook configurations
        3. For each organization:
           - Lists webhooks from Smartsheet
           - Compares with database
           - Registers missing webhooks
           - Re-enables disabled webhooks
           - Detects and updates URL mismatches
           - Deletes zombie/duplicate/stale webhooks
        4. Syncs to dual tables for trigger compatibility
        """
        result = HealthCheckResult()

        log_separator(log, "WEBHOOK HEALTH CHECK")
        log.info(f"[WEBHOOK:HEALTH] Time: {result.timestamp.isoformat()}")

        try:
            # CRITICAL: Verify FastAPI is healthy before attempting any operations
            # Based on v2 pattern - prevents failed webhook registrations
            if not await self._is_fastapi_healthy():
                log.error(
                    "[WEBHOOK:HEALTH] SKIPPING: FastAPI is not responding. "
                    "Cannot register webhooks when endpoint is down. "
                    "Will retry on next check cycle."
                )
                result.skipped_no_fastapi = True
                return result

            log.info("[WEBHOOK:HEALTH] FastAPI health check passed")

            # Get all active webhook-mode configurations
            configs = self._get_active_webhook_configs()
            if not configs:
                log.info("[WEBHOOK:HEALTH] No active webhook configurations")
                return result

            log.info(f"[WEBHOOK:HEALTH] Found {len(configs)} active configurations")

            # Group by organization
            orgs = self._group_configs_by_org(configs)
            result.organizations_checked = len(orgs)

            log.info(f"[WEBHOOK:HEALTH] Processing {len(orgs)} organizations")

            # SH-178: cross-org ownership context, fetched once per cycle.
            # Smartsheet's list-webhooks returns everything owned by the
            # token's USER, so when one Smartsheet account backs multiple
            # orgs each org "sees" the others' webhooks. Deletion below is
            # gated on provable ownership (imagetocell_webhooks row).
            global_webhook_owners = self._get_global_webhook_owners()
            global_active_sheets = self._get_global_active_sheets()

            # Process each organization
            for org_id, org_configs in orgs.items():
                try:
                    org_result = await self._process_organization(
                        org_id,
                        org_configs,
                        global_webhook_owners=global_webhook_owners,
                        global_active_sheets=global_active_sheets,
                    )

                    # Aggregate results
                    result.webhooks_checked += org_result.webhooks_checked
                    result.webhooks_healthy += org_result.webhooks_healthy
                    result.webhooks_registered += org_result.webhooks_registered
                    result.webhooks_re_enabled += org_result.webhooks_re_enabled
                    result.webhooks_url_updated += org_result.webhooks_url_updated
                    result.webhooks_waiting += org_result.webhooks_waiting
                    result.zombies_deleted += org_result.zombies_deleted
                    result.duplicates_deleted += org_result.duplicates_deleted
                    result.stale_deleted += org_result.stale_deleted
                    result.unowned_skipped += org_result.unowned_skipped
                    result.errors += org_result.errors

                except Exception as e:
                    log.error(f"[WEBHOOK:HEALTH] Error processing org {org_id}: {e}")
                    result.errors += 1

        except Exception as e:
            log.error(f"[WEBHOOK:HEALTH] Health check failed: {e}")
            result.errors += 1

        return result

    def _get_active_webhook_configs(self) -> List[Dict]:
        """Get active configurations exclusively managed by v3.

        Queries configs matching WEBHOOK_PROCESSING_MODES (default: "webhook_v3").
        Set to "webhook,webhook_v3" when deploying v3 as the sole backend.
        """
        try:
            modes = self._settings.webhook_processing_modes_list
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.CONFIG)
                    .select("*")
                    .eq("paused", False)
                    .in_("processing_mode", modes)
                    .execute(),
                "get active webhook configs"
            )
            # SH-178B: skip configs whose runner lost OAuth and has no failover.
            rows = result.data or []
            return [
                r for r in rows
                if (r.get("connection_status") or "active") != "needs_connection"
            ]
        except Exception as e:
            log.error(f"[WEBHOOK:HEALTH] Failed to get configs: {e}")
            return []

    def _group_configs_by_org(self, configs: List[Dict]) -> Dict[str, List[Dict]]:
        """Group configurations by organization ID."""
        orgs = {}
        for config in configs:
            org_id = config.get("organization_id")
            if org_id:
                if org_id not in orgs:
                    orgs[org_id] = []
                orgs[org_id].append(config)
        return orgs

    async def _process_organization(
        self,
        org_id: str,
        configs: List[Dict],
        global_webhook_owners: Optional[Dict[int, str]] = None,
        global_active_sheets: Optional[Set[int]] = None,
    ) -> HealthCheckResult:
        """
        Process all webhooks for a single organization.

        Args:
            org_id: Organization ID
            configs: List of configs for this organization
            global_webhook_owners: SH-178 — webhook_id -> organization_id across
                ALL orgs (from imagetocell_webhooks). ``None`` means the lookup
                failed; every deletion is skipped this cycle (fail closed).
            global_active_sheets: SH-178 — worksheet_ids with ANY active config
                across ALL orgs. ``None`` means the lookup failed (fail closed).

        Returns:
            HealthCheckResult for this organization
        """
        result = HealthCheckResult()

        # SH-178 (part B): group configs by runner identity. Smartsheet's
        # list-webhooks endpoint is scoped to the token's USER, so with two
        # runners in one org a single "first creator" pass literally cannot
        # see (or recover) the other runner's webhooks. Each identity group
        # gets its own token/webhook service and its own pass; the expected
        # sheet set stays ORG-wide so one runner's sheets are never treated
        # as "wrong worksheet" zombies by another runner's pass.
        expected_sheets = {c["worksheet_id"] for c in configs}
        groups: Dict[Optional[str], List[Dict]] = {}
        for config in configs:
            runner = config.get("runner_user_id") or config.get("created_by")
            groups.setdefault(runner, []).append(config)

        for runner_user_id, group_configs in groups.items():
            group_result = await self._process_identity_group(
                org_id=org_id,
                runner_user_id=runner_user_id,
                configs=group_configs,
                expected_sheets=expected_sheets,
                global_webhook_owners=global_webhook_owners,
                global_active_sheets=global_active_sheets,
            )
            result.webhooks_checked += group_result.webhooks_checked
            result.webhooks_healthy += group_result.webhooks_healthy
            result.webhooks_registered += group_result.webhooks_registered
            result.webhooks_re_enabled += group_result.webhooks_re_enabled
            result.webhooks_url_updated += group_result.webhooks_url_updated
            result.webhooks_waiting += group_result.webhooks_waiting
            result.zombies_deleted += group_result.zombies_deleted
            result.duplicates_deleted += group_result.duplicates_deleted
            result.stale_deleted += group_result.stale_deleted
            result.unowned_skipped += group_result.unowned_skipped
            result.errors += group_result.errors

        return result

    async def _process_identity_group(
        self,
        org_id: str,
        runner_user_id: Optional[str],
        configs: List[Dict],
        expected_sheets: Set[int],
        global_webhook_owners: Optional[Dict[int, str]] = None,
        global_active_sheets: Optional[Set[int]] = None,
    ) -> HealthCheckResult:
        """
        Process the webhooks of one runner identity within an organization.

        SH-178 (part B): one pass per runner — the webhook service is built
        with the runner's token, so this pass sees exactly the webhooks that
        Smartsheet user owns. ``expected_sheets`` covers the WHOLE org so
        sheets monitored by other runners are not zombie candidates here.
        """
        result = HealthCheckResult()
        flow = FlowLogger(log, "WEBHOOK:ORG", org_id="[REDACTED]")

        flow.start(
            f"Processing {len(configs)} configurations "
            f"(runner={'[set]' if runner_user_id else '[none]'})"
        )

        try:
            # SH-100: WebhookService is now async, backed by smarthub_core's
            # SmartsheetClient (cluster-wide rate limit + encrypted token cache).
            # SH-178: token belongs to this group's runner — Smartsheet
            # webhooks are owned by the creating token's user.
            webhook_service = SmartsheetWebhookService(
                get_smartsheet_client(org_id, runner_user_id)
            )

            # Get all webhooks from Smartsheet
            flow.step(1, "Listing webhooks from Smartsheet")
            try:
                smartsheet_webhooks = await webhook_service.list_webhooks()
            except Exception as list_exc:
                # SH-178B: authoritative revoke → failover or park this group.
                from src.services.runner_failover import (
                    classify_credential_failure,
                    failover_configs,
                )

                state = classify_credential_failure(list_exc)
                if state == "reconnect_required":
                    log.warning(
                        "[WEBHOOK:ORG] SH-178: runner revoked for org=%s — "
                        "running failover for %d config(s)",
                        org_id,
                        len(configs),
                    )
                    await failover_configs(
                        configs,
                        organization_id=org_id,
                        revoked_runner_user_id=runner_user_id,
                    )
                    flow.fail(list_exc)
                    result.errors += 1
                    return result
                raise

            # Get database webhooks
            db_webhooks = self._get_db_webhooks_for_org(org_id)

            flow.debug(
                f"Smartsheet: {len(smartsheet_webhooks)} webhooks, "
                f"Database: {len(db_webhooks)} records"
            )

            # SH-178: webhooks provably owned by ANOTHER org. Smartsheet's
            # list endpoint is scoped to the token's USER, so when the same
            # Smartsheet account backs multiple orgs each org sees the
            # others' webhooks. Those must never be touched here — not by
            # zombie cleanup, not by duplicate cleanup, not by the
            # registration pre-cleanup.
            foreign_webhook_ids: Set[int] = {
                wid
                for wid, owner in (global_webhook_owners or {}).items()
                if owner != org_id
            }

            # Build lookup maps — foreign webhooks are excluded from the
            # per-config view so recovery decisions (recreate disabled /
            # unverified webhooks) only ever act on our own.
            ss_webhooks_by_sheet: Dict[int, List[SmartsheetWebhook]] = {}
            for wh in smartsheet_webhooks:
                if wh.id in foreign_webhook_ids:
                    log.info(
                        f"[WEBHOOK:HEALTH] SH-178: webhook {wh.id} "
                        f"(sheet {wh.scope_object_id}) belongs to another org — "
                        f"ignoring for this org's checks"
                    )
                    continue
                sheet_id = wh.scope_object_id
                if sheet_id not in ss_webhooks_by_sheet:
                    ss_webhooks_by_sheet[sheet_id] = []
                ss_webhooks_by_sheet[sheet_id].append(wh)

            db_webhooks_by_sheet = {
                w["worksheet_id"]: w for w in db_webhooks
            }

            # Step 2: Check each config's webhook
            flow.step(2, "Checking webhook status for each config")

            for config in configs:
                worksheet_id = config["worksheet_id"]
                config_name = config.get("configuration_name", "unknown")

                result.webhooks_checked += 1

                with log_context(
                    org_id=org_id,
                    worksheet_id=worksheet_id,
                    config_id=config.get("id"),
                ):
                    try:
                        webhook_result = await self._check_webhook_for_config(
                            config=config,
                            webhook_service=webhook_service,
                            ss_webhooks=ss_webhooks_by_sheet.get(worksheet_id, []),
                            db_webhook=db_webhooks_by_sheet.get(worksheet_id),
                            protected_webhook_ids=foreign_webhook_ids,
                        )

                        if webhook_result == "healthy":
                            result.webhooks_healthy += 1
                        elif webhook_result == "registered":
                            result.webhooks_registered += 1
                        elif webhook_result == "re_enabled":
                            result.webhooks_re_enabled += 1
                        elif webhook_result == "url_updated":
                            result.webhooks_url_updated += 1
                        elif webhook_result == "waiting":
                            result.webhooks_waiting += 1
                        elif webhook_result == "error":
                            result.errors += 1

                    except Exception as e:
                        log.error(
                            f"[WEBHOOK:HEALTH] Error checking webhook for "
                            f"{config_name}: {e}"
                        )
                        result.errors += 1

            # Step 3: Clean up zombies (based on v2 - 3 types)
            flow.step(3, "Cleaning up zombie webhooks (3 types)")

            # SH-178: re-fetch DB webhooks before the zombie scan. Step 2 may
            # have adopted a healthy webhook into the DB (case 6) or
            # registered a new one; scanning against the stale snapshot from
            # the top of the cycle would flag those as "stale" and delete
            # them in the same cycle.
            db_webhooks = self._get_db_webhooks_for_org(org_id)

            wrong_worksheet, duplicates, stale, skipped = self._find_zombie_webhooks(
                smartsheet_webhooks=smartsheet_webhooks,
                db_webhooks=db_webhooks,
                expected_sheets=expected_sheets,
                callback_url_patterns=self._settings.webhook_callback_ownership_patterns(),
                org_id=org_id,
                global_webhook_owners=global_webhook_owners,
                global_active_sheets=global_active_sheets,
            )
            result.unowned_skipped += skipped

            # Delete wrong worksheet zombies
            for zombie in wrong_worksheet:
                try:
                    if await webhook_service.delete_webhook(zombie.id):
                        result.zombies_deleted += 1
                        self._delete_webhook_from_db(zombie.id)
                except Exception as e:
                    log.error(f"[WEBHOOK:HEALTH] Failed to delete wrong worksheet zombie: {e}")

            # Delete duplicates
            for zombie in duplicates:
                try:
                    if await webhook_service.delete_webhook(zombie.id):
                        result.duplicates_deleted += 1
                        self._delete_webhook_from_db(zombie.id)
                except Exception as e:
                    log.error(f"[WEBHOOK:HEALTH] Failed to delete duplicate: {e}")

            # Delete stale webhooks
            for zombie in stale:
                try:
                    if await webhook_service.delete_webhook(zombie.id):
                        result.stale_deleted += 1
                        self._delete_webhook_from_db(zombie.id)
                except Exception as e:
                    log.error(f"[WEBHOOK:HEALTH] Failed to delete stale webhook: {e}")

            flow.success(
                "Organization processed",
                healthy=result.webhooks_healthy,
                registered=result.webhooks_registered,
                re_enabled=result.webhooks_re_enabled,
                url_updated=result.webhooks_url_updated,
                waiting=result.webhooks_waiting,
                zombies=result.zombies_deleted,
                duplicates=result.duplicates_deleted,
                stale=result.stale_deleted,
                unowned_skipped=result.unowned_skipped,
            )

        except Exception as e:
            flow.failure("Failed to process organization", e)
            result.errors += 1

        return result

    async def _check_webhook_for_config(
        self,
        config: Dict,
        webhook_service: SmartsheetWebhookService,
        ss_webhooks: List[SmartsheetWebhook],
        db_webhook: Optional[Dict],
        protected_webhook_ids: Optional[Set[int]] = None,
    ) -> str:
        """
        Check and fix webhook for a single configuration.

        Based on v2 robustness patterns including:
        - URL mismatch detection
        - Age check for NEW_NOT_VERIFIED
        - Definitive failure detection

        Args:
            protected_webhook_ids: SH-178 — webhook ids owned by OTHER orgs
                (same Smartsheet user, different org). Never deleted by the
                duplicate cleanup or the registration pre-cleanup.

        Returns:
            Status string: "healthy", "registered", "re_enabled", "url_updated", "waiting", "error"
        """
        worksheet_id = config["worksheet_id"]
        org_id = config["organization_id"]
        config_name = config.get("configuration_name", "unknown")
        expected_url = self._settings.resolve_webhook_callback_url(org_id)
        protected_webhook_ids = protected_webhook_ids or set()

        # Case 1: No webhooks at all - need to register
        if not ss_webhooks:
            log.info(
                f"[WEBHOOK:RECOVERY] No webhook for {config_name}, registering..."
            )
            return await self._register_missing_webhook(
                config, webhook_service, protected_webhook_ids
            )

        # Case 2: Multiple webhooks - clean up duplicates
        if len(ss_webhooks) > 1:
            log.warning(
                f"[WEBHOOK:HEALTH] Found {len(ss_webhooks)} webhooks for "
                f"{config_name}, cleaning up..."
            )
            # Keep the one in database if exists
            keep_id = db_webhook.get("webhook_id") if db_webhook else None
            await webhook_service.cleanup_duplicate_webhooks(
                worksheet_id,
                keep_webhook_id=int(keep_id) if keep_id else None,
                protected_ids=protected_webhook_ids,
            )
            ss_webhooks = [
                wh
                for wh in await webhook_service.get_webhooks_for_worksheet(worksheet_id)
                if wh.id not in protected_webhook_ids
            ]

        # Now we should have exactly one webhook
        webhook = ss_webhooks[0] if ss_webhooks else None

        if not webhook:
            return await self._register_missing_webhook(
                config, webhook_service, protected_webhook_ids
            )

        # Case 3: Webhook exists but is disabled
        if webhook.is_disabled:
            log.warning(
                f"[WEBHOOK:RECOVERY] Webhook {webhook.id} is disabled "
                f"({webhook.status})"
            )

            # CRITICAL: Check for definitive failures (based on v2)
            if webhook.is_definitive_failure:
                log.warning(
                    f"[WEBHOOK:RECOVERY] Webhook {webhook.id} has DEFINITIVE "
                    f"failure ({webhook.status}), recreating immediately..."
                )
                await webhook_service.delete_webhook(webhook.id)
                self._delete_webhook_from_db(webhook.id)
                return await self._register_missing_webhook(
                    config, webhook_service, protected_webhook_ids
                )

            # Try to re-enable
            if await webhook_service.enable_webhook(webhook.id):
                self._update_webhook_status_in_db(webhook.id, "active")
                return "re_enabled"
            else:
                return "error"

        # Case 4: Webhook needs verification (NEW_NOT_VERIFIED)
        if webhook.needs_verification:
            age_minutes = webhook.age_minutes

            # CRITICAL: Age check based on v2 pattern
            # Wait before recreating to allow Smartsheet verification to complete
            if age_minutes is not None and age_minutes < MIN_AGE_FOR_REREGISTER_MINUTES:
                log.info(
                    f"[WEBHOOK:HEALTH] Webhook {webhook.id} pending verification "
                    f"({age_minutes:.1f} min < {MIN_AGE_FOR_REREGISTER_MINUTES} min), "
                    f"will check again next cycle"
                )
                self._update_webhook_timestamp(webhook.id)
                return "waiting"

            # Webhook has been unverified too long - recreate
            age_str = f"{age_minutes:.1f} minutes" if age_minutes else "unknown time"
            log.warning(
                f"[WEBHOOK:RECOVERY] Webhook {webhook.id} has been unverified "
                f"for {age_str}, recreating..."
            )
            await webhook_service.delete_webhook(webhook.id)
            self._delete_webhook_from_db(webhook.id)
            return await self._register_missing_webhook(
                config, webhook_service, protected_webhook_ids
            )

        # Case 5: Check URL mismatch (based on v2)
        if expected_url and webhook.callback_url != expected_url:
            log.warning(
                f"[WEBHOOK:HEALTH] URL mismatch for webhook {webhook.id}: "
                f"{webhook.callback_url} != {expected_url}"
            )
            if await webhook_service.update_callback_url(webhook.id, expected_url):
                # Update database
                self._update_webhook_url_in_db(webhook.id, expected_url)
                return "url_updated"
            else:
                return "error"

        # Case 6: Webhook is healthy
        if webhook.is_healthy:
            # Ensure it's in our database
            if not db_webhook:
                self._save_webhook_to_db(webhook, org_id)

            self._update_webhook_timestamp(webhook.id)
            return "healthy"

        return "error"

    async def _register_missing_webhook(
        self,
        config: Dict,
        webhook_service: SmartsheetWebhookService,
        protected_webhook_ids: Optional[Set[int]] = None,
    ) -> str:
        """
        Register a missing webhook for a configuration.

        Based on v2 pattern: runs backfill for existing attachments BEFORE
        registering the webhook, so existing images are processed.

        SH-178: ``protected_webhook_ids`` (webhooks owned by other orgs) are
        forwarded to the registration pre/post cleanup so it cannot delete
        another org's webhook on the same sheet.
        """
        worksheet_id = config["worksheet_id"]
        org_id = config["organization_id"]
        config_name = config.get("configuration_name", "unknown")

        callback_url = self._settings.resolve_webhook_callback_url(org_id)
        if not callback_url:
            log.error(
                "[WEBHOOK:RECOVERY] No callback URL configured, cannot register"
            )
            return "error"

        try:
            # CRITICAL: Run backfill BEFORE registering webhook (based on v2)
            # This ensures existing attachments are processed before we go live
            try:
                from src.workers.backfill import get_backfill_worker

                log.info(
                    f"[WEBHOOK:RECOVERY] Running initial backfill for existing "
                    f"attachments on worksheet {worksheet_id}..."
                )
                backfill_worker = get_backfill_worker()
                backfill_result = await backfill_worker.run_for_worksheet(
                    worksheet_id
                )

                if backfill_result.error:
                    log.warning(
                        f"[WEBHOOK:RECOVERY] Backfill completed with error: "
                        f"{backfill_result.error}"
                    )
                else:
                    log.info(
                        f"[WEBHOOK:RECOVERY] Backfill complete: "
                        f"{backfill_result.rows_processed} processed, "
                        f"{backfill_result.rows_skipped} skipped"
                    )
            except Exception as backfill_err:
                # Non-critical - continue with webhook registration
                log.warning(
                    f"[WEBHOOK:RECOVERY] Backfill failed (continuing with "
                    f"webhook registration): {backfill_err}"
                )

            # SH-100: register_webhook is now async — no need for the
            # threadpool wrap that used to be required because the legacy
            # sync register_webhook contained blocking time.sleep() calls.
            # The new wrapper awaits on smarthub_core's rate-limit primitive
            # so the event loop stays responsive (FastAPI can answer the
            # Smartsheet verification challenge synchronously).
            webhook = await webhook_service.register_webhook(
                worksheet_id=worksheet_id,
                callback_url=callback_url,
                name=f"Image-to-Cell - {config_name}",
                protected_ids=protected_webhook_ids,
                ownership_patterns=self._settings.webhook_callback_ownership_patterns(),
            )

            # Save to database (both tables)
            self._save_webhook_to_db(webhook, org_id)

            log.info(
                f"[WEBHOOK:RECOVERY] Registered webhook {webhook.id} "
                f"for {config_name}"
            )
            return "registered"

        except Exception as e:
            log.error(f"[WEBHOOK:RECOVERY] Failed to register webhook: {e}")
            return "error"

    def _find_zombie_webhooks(
        self,
        smartsheet_webhooks: List[SmartsheetWebhook],
        db_webhooks: List[Dict],
        expected_sheets: Set[int],
        callback_url_patterns: Optional[List[str]] = None,
        org_id: Optional[str] = None,
        global_webhook_owners: Optional[Dict[int, str]] = None,
        global_active_sheets: Optional[Set[int]] = None,
        *,
        callback_url_pattern: str = "",
    ) -> Tuple[
        List[SmartsheetWebhook],
        List[SmartsheetWebhook],
        List[SmartsheetWebhook],
        int,
    ]:
        """
        Find zombie webhooks that should be deleted.

        Based on v2 pattern, identifies THREE types of zombies:
        1. Wrong worksheet - Webhooks for sheets we don't monitor
        2. Duplicates - Multiple webhooks for the same worksheet
        3. Stale - Webhooks in Smartsheet but not in database (orphans)

        SH-178 ownership gate: Smartsheet's list endpoint is scoped to the
        token's USER, not to our org. When the same Smartsheet account backs
        multiple orgs, this org can see (and before this fix, delete) the
        other orgs' webhooks. Deletion is now allowed only when we can PROVE
        ownership or prove orphanhood:

        - Type 1 (wrong worksheet) requires the webhook id to be registered
          to THIS org in ``imagetocell_webhooks`` (``global_webhook_owners``),
          or — for webhooks in no org's DB — the sheet must have no active
          I2C config in ANY org (``global_active_sheets``). Webhooks owned by
          another org are skipped and logged.
        - Types 2/3 (duplicates / stale) only ever act on sheets this org
          monitors, but any webhook provably owned by another org is skipped.
        - When either global lookup is unavailable (``None``), the cleanup
          FAILS CLOSED: nothing is deleted this cycle and a warning is logged.

        ``callback_url_patterns`` lists substrings that mark a webhook as
        owned by this deployment (gateway prefix and/or legacy direct URL).
        ``callback_url_pattern`` remains as a single-pattern alias for tests.

        Returns:
            Tuple of (wrong_worksheet_zombies, duplicate_zombies,
            stale_zombies, unowned_skipped_count)
        """
        from smarthub_core.platforms.smartsheet import is_our_webhook_callback

        wrong_worksheet: List[SmartsheetWebhook] = []
        duplicates: List[SmartsheetWebhook] = []
        stale: List[SmartsheetWebhook] = []
        unowned_skipped = 0

        patterns: List[str] = list(callback_url_patterns or [])
        if callback_url_pattern:
            patterns.append(callback_url_pattern)

        # SH-178 fail-closed: without the cross-org ownership context we
        # cannot distinguish "our zombie" from "another org's live webhook".
        # Deleting on partial information is exactly the bug this fixes.
        if global_webhook_owners is None or global_active_sheets is None:
            log.warning(
                "[WEBHOOK:ZOMBIE] SH-178: global ownership context unavailable "
                "(owners=%s, active_sheets=%s) — skipping ALL zombie deletion "
                "this cycle (fail closed)",
                "ok" if global_webhook_owners is not None else "missing",
                "ok" if global_active_sheets is not None else "missing",
            )
            return wrong_worksheet, duplicates, stale, unowned_skipped

        db_webhook_ids = {
            int(w["webhook_id"]) for w in db_webhooks if w.get("webhook_id")
        }
        db_worksheet_to_webhook = {
            int(w["worksheet_id"]): int(w["webhook_id"])
            for w in db_webhooks
            if w.get("worksheet_id") and w.get("webhook_id")
        }

        def _owned_by_other_org(webhook_id: int) -> bool:
            owner = global_webhook_owners.get(webhook_id)
            return owner is not None and owner != org_id

        # Group webhooks by worksheet for duplicate detection
        webhooks_by_worksheet: Dict[int, List[SmartsheetWebhook]] = {}

        for wh in smartsheet_webhooks:
            # Only consider webhooks pointing to OUR callback URL.
            # Name-based matching is intentionally avoided because V2 webhooks
            # share the same "Image-to-Cell" name prefix and would otherwise be
            # treated as zombies (wrong worksheet) and deleted.
            is_ours = is_our_webhook_callback(wh.callback_url, patterns)

            if not is_ours:
                continue

            # SH-178: webhook provably registered by another org — the same
            # Smartsheet user backs both orgs, so it shows up in our list.
            # Never a deletion candidate, for any zombie type.
            if _owned_by_other_org(wh.id):
                log.info(
                    f"[WEBHOOK:ZOMBIE] SH-178: skipping webhook {wh.id} "
                    f"(sheet {wh.scope_object_id}) — registered to another org"
                )
                unowned_skipped += 1
                continue

            # Zombie type 1: Wrong worksheet
            if wh.scope_object_id not in expected_sheets:
                if wh.id in db_webhook_ids:
                    # Provably ours (this org's imagetocell_webhooks row) but
                    # the sheet has no active config here → real zombie.
                    log.warning(
                        f"[WEBHOOK:ZOMBIE] Type 1 - Wrong worksheet: "
                        f"Webhook {wh.id} for unexpected sheet {wh.scope_object_id}"
                    )
                    wrong_worksheet.append(wh)
                elif wh.scope_object_id not in global_active_sheets:
                    # In nobody's DB and the sheet has no active I2C config in
                    # ANY org → true orphan (e.g. config deleted while the
                    # webhook survived). Safe to collect.
                    log.warning(
                        f"[WEBHOOK:ZOMBIE] Type 1 - Orphan: Webhook {wh.id} "
                        f"for sheet {wh.scope_object_id} is in no org's DB and "
                        f"no org has an active config for that sheet"
                    )
                    wrong_worksheet.append(wh)
                else:
                    # Not provably ours and some org actively monitors the
                    # sheet — almost certainly another org's live webhook
                    # (e.g. registered by lifecycle before its DB row synced).
                    # Log, count, leave alone.
                    log.info(
                        f"[WEBHOOK:ZOMBIE] SH-178: NOT deleting webhook {wh.id} "
                        f"(sheet {wh.scope_object_id}) — not registered to this "
                        f"org and the sheet has an active config in another org"
                    )
                    unowned_skipped += 1
                continue

            # Group for duplicate detection
            ws_id = wh.scope_object_id
            if ws_id not in webhooks_by_worksheet:
                webhooks_by_worksheet[ws_id] = []
            webhooks_by_worksheet[ws_id].append(wh)

        # Detect duplicates and stale webhooks
        for ws_id, ws_webhooks in webhooks_by_worksheet.items():
            expected_webhook_id = db_worksheet_to_webhook.get(ws_id)

            if len(ws_webhooks) > 1:
                # Multiple webhooks for same worksheet
                log.warning(
                    f"[WEBHOOK:ZOMBIE] Type 2 - Duplicates: "
                    f"Found {len(ws_webhooks)} webhooks for worksheet {ws_id}"
                )

                for wh in ws_webhooks:
                    if expected_webhook_id and wh.id == expected_webhook_id:
                        # This is the expected one, keep it
                        log.debug(f"  Keeping webhook {wh.id} (matches database)")
                    else:
                        # Duplicate or no expected webhook
                        log.warning(f"  Marking webhook {wh.id} as duplicate")
                        duplicates.append(wh)

            elif len(ws_webhooks) == 1:
                # Single webhook - check if stale
                wh = ws_webhooks[0]
                if wh.id not in db_webhook_ids:
                    log.warning(
                        f"[WEBHOOK:ZOMBIE] Type 3 - Stale: "
                        f"Webhook {wh.id} for worksheet {ws_id} not in database"
                    )
                    stale.append(wh)

        return wrong_worksheet, duplicates, stale, unowned_skipped

    # =========================================================================
    # Database Operations
    # =========================================================================

    def _get_global_webhook_owners(self) -> Optional[Dict[int, str]]:
        """SH-178: map webhook_id -> organization_id across ALL orgs.

        Used to prove ownership before any deletion. Returns ``None`` on
        failure so callers fail closed (skip all deletion this cycle)
        instead of deleting on partial information.
        """
        try:
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .select("webhook_id, organization_id")
                    .execute(),
                "get global webhook owners"
            )
            owners: Dict[int, str] = {}
            for row in result.data or []:
                webhook_id = row.get("webhook_id")
                org_id = row.get("organization_id")
                if webhook_id and org_id:
                    try:
                        owners[int(webhook_id)] = org_id
                    except (TypeError, ValueError):
                        log.warning(
                            f"[WEBHOOK:DB] SH-178: non-numeric webhook_id "
                            f"{webhook_id!r} in {TABLE_NAMES.WEBHOOKS}, skipping"
                        )
            return owners
        except Exception as e:
            log.error(f"[WEBHOOK:DB] SH-178: failed to get global webhook owners: {e}")
            return None

    def _get_global_active_sheets(self) -> Optional[Set[int]]:
        """SH-178: worksheet_ids with ANY active (non-paused) webhook-mode
        config across ALL orgs.

        Safety net for true orphans: a webhook whose sheet appears here may
        belong to another org even if we cannot see its DB row yet, so it is
        never deleted. Returns ``None`` on failure (callers fail closed).
        """
        try:
            modes = self._settings.webhook_processing_modes_list
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.CONFIG)
                    .select("worksheet_id")
                    .eq("paused", False)
                    .in_("processing_mode", modes)
                    .execute(),
                "get global active sheets"
            )
            sheets: Set[int] = set()
            for row in result.data or []:
                worksheet_id = row.get("worksheet_id")
                if worksheet_id:
                    try:
                        sheets.add(int(worksheet_id))
                    except (TypeError, ValueError):
                        pass
            return sheets
        except Exception as e:
            log.error(f"[WEBHOOK:DB] SH-178: failed to get global active sheets: {e}")
            return None

    def _get_db_webhooks_for_org(self, org_id: str) -> List[Dict]:
        """Get all webhook records from database for an organization."""
        try:
            result = self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .select("*")
                    .eq("organization_id", org_id)
                    .execute(),
                f"get webhooks for org {org_id}"
            )
            return result.data or []
        except Exception as e:
            log.error(f"[WEBHOOK:DB] Failed to get webhooks: {e}")
            return []

    def _save_webhook_to_db(
        self,
        webhook: SmartsheetWebhook,
        org_id: str,
    ) -> None:
        """
        Save webhook to database.

        SH-230 D: platform_webhook_registrations is write SoT (required).
        imagetocell_webhooks + worksheet_webhooks are soft-fail mirrors.
        """
        try:
            now = datetime.utcnow().isoformat()

            # SH-108: encrypt the shared_secret before persisting. Without this
            # the recovery path silently overwrites the encrypted row that
            # WebhookRepository.save() (or the SH-108 backfill script) put in
            # place, re-introducing plaintext secrets on every webhook
            # re-registration.
            encrypted_secret = _ensure_encrypted_secret(webhook.shared_secret)

            from smarthub_core.webhooks.platform_registrations import (
                DEFAULT_PROVIDER,
                upsert_platform_registration,
            )
            from src.config.constants import PRODUCT_SLUG

            upsert_platform_registration(
                self._db,
                provider=DEFAULT_PROVIDER,
                tool_slug=PRODUCT_SLUG,
                organization_id=org_id,
                worksheet_id=int(webhook.scope_object_id),
                webhook_id=int(webhook.id),
                shared_secret_ciphertext=encrypted_secret,
                callback_url=webhook.callback_url,
                status="active",
                soft_fail=False,
            )

            data = {
                "webhook_id": str(webhook.id),
                "worksheet_id": webhook.scope_object_id,
                "organization_id": org_id,
                "webhook_url": webhook.callback_url,
                "shared_secret": encrypted_secret,
                "status": "active",
                "updated_at": now,
            }

            try:
                self._db.execute_with_retry(
                    lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                        .upsert(data, on_conflict="worksheet_id")
                        .execute(),
                    "save webhook (tool mirror)"
                )
                log.debug(f"[WEBHOOK:DB] Mirrored to {TABLE_NAMES.WEBHOOKS}")
            except Exception as mirror_err:
                log.warning(
                    "[WEBHOOK:DB] tool-table mirror soft-fail sheet=%s: %s",
                    webhook.scope_object_id,
                    mirror_err,
                )

            try:
                secondary_data = {
                    "worksheet_id": webhook.scope_object_id,
                    "webhook_id": str(webhook.id),
                    "webhook_url": webhook.callback_url,
                    "organization_id": org_id,
                    "shared_secret": encrypted_secret,
                    "webhook_enabled": True,
                    "active_config_count": 1,
                    "updated_at": now,
                }
                self._db.execute_with_retry(
                    lambda: self._db.table(WORKSHEET_WEBHOOKS_TABLE)
                        .upsert(secondary_data, on_conflict="worksheet_id")
                        .execute(),
                    "save webhook (secondary)"
                )
                log.debug(f"[WEBHOOK:DB] Synced to {WORKSHEET_WEBHOOKS_TABLE}")
            except Exception as sync_err:
                log.warning(
                    f"[WEBHOOK:DB] Could not sync to {WORKSHEET_WEBHOOKS_TABLE} "
                    f"(non-critical): {sync_err}"
                )

        except Exception as e:
            log.error(f"[WEBHOOK:DB] Failed to save webhook: {e}")
            raise

    def _update_webhook_status_in_db(
        self,
        webhook_id: int,
        status: str,
    ) -> None:
        """Update webhook status in database."""
        try:
            now = datetime.utcnow().isoformat()
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .update({"status": status, "updated_at": now})
                    .eq("webhook_id", str(webhook_id))
                    .execute(),
                "update webhook status"
            )
        except Exception as e:
            log.error(f"[WEBHOOK:DB] Failed to update status: {e}")

    def _update_webhook_timestamp(self, webhook_id: int) -> None:
        """Update webhook's last checked timestamp."""
        try:
            now = datetime.utcnow().isoformat()
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .update({"updated_at": now})
                    .eq("webhook_id", str(webhook_id))
                    .execute(),
                "update webhook timestamp"
            )
        except Exception as e:
            log.debug(f"[WEBHOOK:DB] Failed to update timestamp: {e}")

    def _update_webhook_url_in_db(
        self,
        webhook_id: int,
        new_url: str,
    ) -> None:
        """Update webhook callback URL in platform + tool tables."""
        try:
            now = datetime.utcnow().isoformat()
            from smarthub_core.webhooks.platform_registrations import (
                DEFAULT_PROVIDER,
                PLATFORM_WEBHOOKS_TABLE,
                upsert_platform_registration,
            )
            from src.config.constants import PRODUCT_SLUG

            try:
                result = self._db.execute_with_retry(
                    lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                        .select(
                            "organization_id, worksheet_id, webhook_id, "
                            "shared_secret, status"
                        )
                        .eq("webhook_id", str(webhook_id))
                        .limit(1)
                        .execute(),
                    f"load webhook {webhook_id} for URL update",
                )
                rows = list(result.data or [])
            except Exception:
                rows = []

            if rows:
                row = rows[0]
                upsert_platform_registration(
                    self._db,
                    provider=DEFAULT_PROVIDER,
                    tool_slug=PRODUCT_SLUG,
                    organization_id=str(row["organization_id"]),
                    worksheet_id=int(row["worksheet_id"]),
                    webhook_id=int(webhook_id),
                    shared_secret_ciphertext=row.get("shared_secret"),
                    callback_url=new_url,
                    status=row.get("status") or "active",
                    soft_fail=False,
                )
            else:
                self._db.execute_with_retry(
                    lambda: self._db.table(PLATFORM_WEBHOOKS_TABLE)
                        .update({"callback_url": new_url, "updated_at": now})
                        .eq("provider", DEFAULT_PROVIDER)
                        .eq("tool_slug", PRODUCT_SLUG)
                        .eq("webhook_id", int(webhook_id))
                        .execute(),
                    f"patch platform callback_url webhook={webhook_id}",
                )

            try:
                self._db.execute_with_retry(
                    lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                        .update({"webhook_url": new_url, "updated_at": now})
                        .eq("webhook_id", str(webhook_id))
                        .execute(),
                    "update webhook URL (tool mirror)"
                )
            except Exception as mirror_err:
                log.warning(
                    "[WEBHOOK:DB] tool-table URL mirror soft-fail webhook=%s: %s",
                    webhook_id,
                    mirror_err,
                )

            try:
                self._db.execute_with_retry(
                    lambda: self._db.table(WORKSHEET_WEBHOOKS_TABLE)
                        .update({"webhook_url": new_url, "updated_at": now})
                        .eq("webhook_id", str(webhook_id))
                        .execute(),
                    "update webhook URL (secondary)"
                )
            except Exception:
                pass

        except Exception as e:
            log.error(f"[WEBHOOK:DB] Failed to update URL: {e}")
            raise

    def _delete_webhook_from_db(self, webhook_id: int) -> None:
        """Delete webhook from platform (required) and tool table (soft-fail)."""
        from smarthub_core.webhooks.platform_registrations import (
            DEFAULT_PROVIDER,
            delete_platform_registration,
        )
        from src.config.constants import PRODUCT_SLUG

        try:
            delete_platform_registration(
                self._db,
                provider=DEFAULT_PROVIDER,
                tool_slug=PRODUCT_SLUG,
                webhook_id=int(webhook_id),
                soft_fail=False,
            )
        except Exception as e:
            log.error(
                f"[WEBHOOK:DB] Failed to delete platform webhook {webhook_id}: {e}"
            )
            raise

        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.WEBHOOKS)
                    .delete()
                    .eq("webhook_id", str(webhook_id))
                    .execute(),
                f"delete webhook {webhook_id}"
            )
        except Exception as e:
            log.warning(
                "[WEBHOOK:DB] tool-table delete soft-fail webhook=%s: %s",
                webhook_id,
                e,
            )


# =============================================================================
# Singleton
# =============================================================================

_lifecycle_manager: Optional[WebhookLifecycleManager] = None


def get_webhook_lifecycle_manager() -> WebhookLifecycleManager:
    """Get the global webhook lifecycle manager singleton."""
    global _lifecycle_manager
    if _lifecycle_manager is None:
        settings = get_settings()
        _lifecycle_manager = WebhookLifecycleManager(
            check_interval_seconds=settings.webhook_lifecycle_interval_s
        )
    return _lifecycle_manager


async def start_lifecycle_manager() -> None:
    """Start the webhook lifecycle manager."""
    manager = get_webhook_lifecycle_manager()
    await manager.start()


async def stop_lifecycle_manager() -> None:
    """Stop the webhook lifecycle manager."""
    manager = get_webhook_lifecycle_manager()
    await manager.stop()
