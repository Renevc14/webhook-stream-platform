"""Smartsheet webhook CRUD / lifecycle helpers (SH-193 / SH-207).

Async wrapper around the Smartsheet Webhooks API, routed through
``SmartsheetClient.call_with_retry_async`` so cluster-wide rate limit,
encrypted token cache, and retry coverage apply to create / enable /
list / delete — the flakiest Smartsheet endpoints.

Extracted from I2C ``SmartsheetWebhookService`` so the Webhook Gateway
lifecycle manager and other tools can share one implementation. HMAC
verification stays in ``webhook.py`` (ingress path); this module owns
registration and maintenance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional, Set

import smartsheet  # type: ignore[import-untyped]

from smarthub_core.platforms.smartsheet.callback_urls import is_our_webhook_callback
from smarthub_core.platforms.smartsheet.client import SmartsheetClient

log = logging.getLogger(__name__)

SHARED_SECRET_RETRIES = 5
SHARED_SECRET_RETRY_DELAY_S = 6


def _register_ownership_patterns(
    callback_url: str,
    ownership_patterns: Optional[List[str]] = None,
) -> List[str]:
    """Patterns used to decide which sheet webhooks this product may delete.

    Always includes the callback URL being registered so other tools on the
    same sheet survive pre/post cleanup.
    """
    patterns = [p for p in (ownership_patterns or []) if p]
    cb = (callback_url or "").strip()
    if cb and cb not in patterns:
        patterns.append(cb)
    return patterns


class WebhookStatus(str, Enum):
    """Smartsheet webhook statuses."""

    NEW_NOT_VERIFIED = "NEW_NOT_VERIFIED"
    ENABLED = "ENABLED"
    ENABLED_WITH_PROBLEMS = "ENABLED_WITH_PROBLEMS"
    DISABLED_CALLBACK_FAILED = "DISABLED_CALLBACK_FAILED"
    DISABLED_VERIFICATION_FAILED = "DISABLED_VERIFICATION_FAILED"
    DISABLED_ADMINISTRATIVE = "DISABLED_ADMINISTRATIVE"
    DISABLED_SCOPE_INACCESSIBLE = "DISABLED_SCOPE_INACCESSIBLE"


@dataclass
class SmartsheetWebhook:
    """Webhook data from the Smartsheet API."""

    id: int
    name: str
    scope_object_id: int  # worksheet_id
    callback_url: str
    shared_secret: str
    enabled: bool
    status: str
    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    @property
    def is_healthy(self) -> bool:
        return self.enabled and self.status in [
            WebhookStatus.ENABLED,
            WebhookStatus.ENABLED_WITH_PROBLEMS,
        ]

    @property
    def is_disabled(self) -> bool:
        return self.status.startswith("DISABLED_")

    @property
    def needs_verification(self) -> bool:
        return self.status == WebhookStatus.NEW_NOT_VERIFIED

    @property
    def is_definitive_failure(self) -> bool:
        return self.status in [
            WebhookStatus.DISABLED_VERIFICATION_FAILED,
            WebhookStatus.DISABLED_SCOPE_INACCESSIBLE,
            WebhookStatus.DISABLED_ADMINISTRATIVE,
        ]

    @property
    def age_minutes(self) -> Optional[float]:
        if not self.created_at:
            return None
        try:
            created_str = self.created_at
            if created_str.endswith("Z"):
                created_str = created_str[:-1] + "+00:00"
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            return (now_dt - created_dt).total_seconds() / 60
        except Exception:
            return None

    @classmethod
    def from_api_response(cls, data: Any) -> "SmartsheetWebhook":
        """Build the model from a raw Smartsheet SDK response (dict or object)."""
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        elif hasattr(data, "id"):
            data = {
                "id": data.id,
                "name": getattr(data, "name", ""),
                "scopeObjectId": (
                    getattr(data, "scope_object_id", None)
                    or getattr(data, "scopeObjectId", None)
                ),
                "callbackUrl": (
                    getattr(data, "callback_url", "")
                    or getattr(data, "callbackUrl", "")
                ),
                "sharedSecret": (
                    getattr(data, "shared_secret", "")
                    or getattr(data, "sharedSecret", "")
                ),
                "enabled": getattr(data, "enabled", False),
                "status": getattr(data, "status", ""),
                "createdAt": (
                    getattr(data, "created_at", None)
                    or getattr(data, "createdAt", None)
                ),
                "modifiedAt": (
                    getattr(data, "modified_at", None)
                    or getattr(data, "modifiedAt", None)
                ),
            }

        shared_secret = (
            data.get("sharedSecret", "")
            or data.get("shared_secret", "")
            or ""
        )

        return cls(
            id=data.get("id"),
            name=data.get("name", ""),
            scope_object_id=data.get("scopeObjectId") or data.get("scope_object_id"),
            callback_url=data.get("callbackUrl", "") or data.get("callback_url", ""),
            shared_secret=shared_secret,
            enabled=data.get("enabled", False),
            status=data.get("status", ""),
            created_at=data.get("createdAt") or data.get("created_at"),
            modified_at=data.get("modifiedAt") or data.get("modified_at"),
        )


class SmartsheetWebhookService:
    """Async wrapper for Smartsheet webhook management."""

    def __init__(self, client: SmartsheetClient):
        """Bind to a smarthub_core ``SmartsheetClient``."""
        self._client = client
        # Cached list_webhooks response so registration + cleanup paths can
        # reuse it within a short window without re-hitting the API.
        self._list_cache: Optional[List[SmartsheetWebhook]] = None
        self._list_cache_time: float = 0

    # ----- list / get -----------------------------------------------------

    async def list_webhooks(self, max_age_s: float = 0) -> List[SmartsheetWebhook]:
        """List every webhook for this account.

        Args:
            max_age_s: If > 0, return the cached result when younger than
                ``max_age_s`` seconds.
        """
        if max_age_s > 0 and self._list_cache is not None:
            now = asyncio.get_event_loop().time()
            if now - self._list_cache_time < max_age_s:
                log.debug(
                    "[WEBHOOK:LIST] Using cached list (%s webhooks)",
                    len(self._list_cache),
                )
                return self._list_cache

        log.info("[WEBHOOK:LIST] Fetching all webhooks from Smartsheet")
        try:
            # Smartsheet deprecated includeAll on /webhooks (errorCode 1018).
            all_webhooks: List[SmartsheetWebhook] = []
            page = 1
            page_size = 100

            while True:
                current_page = page
                response = await self._client.call_with_retry_async(
                    lambda c, p=current_page: c.Webhooks.list_webhooks(
                        page_size=page_size,
                        page=p,
                    ),
                    description=f"list_webhooks(page={current_page})",
                )
                data = response.data if hasattr(response, "data") else response
                if not data:
                    break

                for item in data:
                    try:
                        webhook = SmartsheetWebhook.from_api_response(item)
                        all_webhooks.append(webhook)
                    except Exception as e:
                        log.warning("[WEBHOOK:LIST] Failed to parse webhook: %s", e)

                if len(data) < page_size:
                    break
                page += 1
                if page > 100:
                    log.warning(
                        "[WEBHOOK:LIST] Reached page=100 safety cap, aborting"
                    )
                    break

            self._list_cache = all_webhooks
            self._list_cache_time = asyncio.get_event_loop().time()
            log.info(
                "[WEBHOOK:LIST] Found %s webhooks (%s pages)",
                len(all_webhooks),
                page,
            )
            return all_webhooks

        except Exception as e:
            log.error("[WEBHOOK:LIST] Failed to list webhooks: %s", e)
            raise

    async def get_webhook(self, webhook_id: int) -> Optional[SmartsheetWebhook]:
        """Fetch a specific webhook by ID. Returns ``None`` on not-found."""
        try:
            response = await self._client.call_with_retry_async(
                lambda c: c.Webhooks.get_webhook(webhook_id),
                description=f"get_webhook({webhook_id})",
            )

            if hasattr(response, "data") and response.data:
                data = response.data
            elif hasattr(response, "result") and response.result:
                data = response.result
            else:
                data = response

            if data:
                return SmartsheetWebhook.from_api_response(data)
            return None

        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "1006" in error_msg:
                return None
            raise

    async def get_webhooks_for_worksheet(
        self, worksheet_id: int
    ) -> List[SmartsheetWebhook]:
        """Get every webhook bound to ``worksheet_id``."""
        all_webhooks = await self.list_webhooks()
        return [w for w in all_webhooks if w.scope_object_id == worksheet_id]

    # ----- registration ---------------------------------------------------

    async def register_webhook(
        self,
        worksheet_id: int,
        callback_url: str,
        name: Optional[str] = None,
        cleanup_existing: bool = True,
        protected_ids: Optional[Set[int]] = None,
        ownership_patterns: Optional[List[str]] = None,
    ) -> SmartsheetWebhook:
        """Register a new webhook for a worksheet.

        1. Pre-cleanup: delete **our** existing webhooks on the worksheet.
        2. Create the webhook.
        3. Enable it.
        4. Capture shared secret from create / enable response
           (``get_webhook`` does not return it).
        5. Post-cleanup: delete race-condition duplicates **of ours**.

        ``protected_ids`` — webhook ids owned by other orgs (SH-178).
        ``ownership_patterns`` — callback URL substrings this product owns.
        """
        log.info(
            "[WEBHOOK:REGISTER] Registering webhook worksheet_id=%s",
            worksheet_id,
        )

        if name is None:
            name = f"Smarthub Webhook - {worksheet_id}"
        protected_ids = protected_ids or set()
        ownership_patterns = _register_ownership_patterns(
            callback_url, ownership_patterns
        )

        try:
            if cleanup_existing:
                deleted_count = await self.cleanup_all_webhooks_for_worksheet(
                    worksheet_id,
                    protected_ids=protected_ids,
                    ownership_patterns=ownership_patterns,
                )
                if deleted_count > 0:
                    log.info(
                        "[WEBHOOK:REGISTER] Pre-cleanup removed %s webhook(s)",
                        deleted_count,
                    )

            webhook_spec = smartsheet.models.Webhook(
                {
                    "name": name,
                    "callbackUrl": callback_url,
                    "scope": "sheet",
                    "scopeObjectId": worksheet_id,
                    "events": ["*.*"],
                    "version": 1,
                }
            )

            response = await self._client.call_with_retry_async(
                lambda c, spec=webhook_spec: c.Webhooks.create_webhook(spec),
                description=f"create_webhook(ws={worksheet_id})",
            )

            if not response.result:
                raise Exception("No result in create webhook response")

            webhook = SmartsheetWebhook.from_api_response(response.result)
            webhook_id = webhook.id
            initial_secret = webhook.shared_secret

            if initial_secret:
                log.info(
                    "[WEBHOOK:REGISTER] Captured shared secret from create "
                    "response webhook_id=%s",
                    webhook_id,
                )
            else:
                log.warning(
                    "[WEBHOOK:REGISTER] No shared_secret in create response "
                    "webhook_id=%s — will try enable response",
                    webhook_id,
                )

            # Do NOT poll / sleep — Smartsheet sends the verification
            # challenge synchronously after enable.
            enable_spec = smartsheet.models.Webhook({"enabled": True})
            enable_response = await self._client.call_with_retry_async(
                lambda c, eid=webhook_id, espec=enable_spec: c.Webhooks.update_webhook(
                    eid, espec
                ),
                description=f"enable_webhook({webhook_id})",
            )

            if (
                not initial_secret
                and hasattr(enable_response, "result")
                and enable_response.result
            ):
                try:
                    enable_wh = SmartsheetWebhook.from_api_response(
                        enable_response.result
                    )
                    if enable_wh.shared_secret:
                        initial_secret = enable_wh.shared_secret
                        log.info(
                            "[WEBHOOK:REGISTER] Captured shared secret from "
                            "enable response webhook_id=%s",
                            webhook_id,
                        )
                except Exception as e:
                    log.debug(
                        "[WEBHOOK:REGISTER] Could not parse enable response "
                        "for secret: %s",
                        e,
                    )

            shared_secret = initial_secret
            if not shared_secret:
                log.warning(
                    "[WEBHOOK:REGISTER] Shared secret missing in create/enable; "
                    "inline recovery will obtain it on first event. "
                    "webhook_id=%s",
                    webhook_id,
                )

            webhook.shared_secret = shared_secret

            self._invalidate_list_cache()
            all_webhooks = await self.get_webhooks_for_worksheet(worksheet_id)
            duplicates_deleted = 0
            for existing in all_webhooks:
                if existing.id == webhook_id:
                    continue
                if existing.id in protected_ids:
                    log.debug(
                        "[WEBHOOK:REGISTER] Skipping webhook %s "
                        "(owned by another org)",
                        existing.id,
                    )
                    continue
                if ownership_patterns and not is_our_webhook_callback(
                    existing.callback_url or "", ownership_patterns
                ):
                    log.debug(
                        "[WEBHOOK:REGISTER] Skipping webhook %s "
                        "(foreign callback %s)",
                        existing.id,
                        existing.callback_url,
                    )
                    continue
                try:
                    if await self.delete_webhook(existing.id):
                        duplicates_deleted += 1
                except Exception:
                    pass

            if duplicates_deleted > 0:
                log.info(
                    "[WEBHOOK:REGISTER] Post-cleanup deleted %s duplicate(s)",
                    duplicates_deleted,
                )

            log.info(
                "[WEBHOOK:REGISTER] Registered webhook_id=%s secret_captured=%s",
                webhook_id,
                bool(shared_secret),
            )
            return webhook

        except Exception as e:
            log.error(
                "[WEBHOOK:REGISTER] Failed for worksheet_id=%s: %s",
                worksheet_id,
                e,
            )
            raise

    async def _fetch_shared_secret_with_retries(
        self,
        webhook_id: int,
        retries: int = SHARED_SECRET_RETRIES,
        delay_seconds: int = SHARED_SECRET_RETRY_DELAY_S,
    ) -> Optional[str]:
        """Retry-loop the shared-secret read after webhook creation.

        Kept for manual recovery tooling — production register captures the
        secret from create/enable responses instead.
        """
        log.info(
            "[WEBHOOK:SECRET] Fetching shared secret webhook_id=%s max=%s",
            webhook_id,
            retries,
        )

        for attempt in range(1, retries + 1):
            try:
                webhook = await self.get_webhook(webhook_id)
                if webhook and webhook.shared_secret:
                    log.info(
                        "[WEBHOOK:SECRET] Retrieved on attempt %s webhook_id=%s",
                        attempt,
                        webhook_id,
                    )
                    return webhook.shared_secret

                log.warning(
                    "[WEBHOOK:SECRET] Attempt %s/%s: shared_secret not present yet",
                    attempt,
                    retries,
                )
            except Exception as e:
                log.warning(
                    "[WEBHOOK:SECRET] Attempt %s/%s error: %s",
                    attempt,
                    retries,
                    e,
                )

            if attempt < retries:
                await asyncio.sleep(delay_seconds)

        log.error(
            "[WEBHOOK:SECRET] Could not retrieve shared secret after %s attempts",
            retries,
        )
        return None

    # ----- enable / disable / update -------------------------------------

    async def enable_webhook(self, webhook_id: int) -> bool:
        """Enable a webhook."""
        try:
            spec = smartsheet.models.Webhook({"enabled": True})
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id, s=spec: c.Webhooks.update_webhook(wid, s),
                description=f"enable_webhook({webhook_id})",
            )
            self._invalidate_list_cache()
            log.info("[WEBHOOK:ENABLE] Enabled webhook_id=%s", webhook_id)
            return True
        except Exception as e:
            log.error(
                "[WEBHOOK:ENABLE] Failed webhook_id=%s: %s", webhook_id, e
            )
            return False

    async def disable_webhook(self, webhook_id: int) -> bool:
        """Disable a webhook."""
        try:
            spec = smartsheet.models.Webhook({"enabled": False})
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id, s=spec: c.Webhooks.update_webhook(wid, s),
                description=f"disable_webhook({webhook_id})",
            )
            self._invalidate_list_cache()
            log.info("[WEBHOOK:DISABLE] Disabled webhook_id=%s", webhook_id)
            return True
        except Exception as e:
            log.error(
                "[WEBHOOK:DISABLE] Failed webhook_id=%s: %s", webhook_id, e
            )
            return False

    async def update_callback_url(self, webhook_id: int, new_url: str) -> bool:
        """Update a webhook's callback URL in place."""
        try:
            spec = smartsheet.models.Webhook({"callbackUrl": new_url})
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id, s=spec: c.Webhooks.update_webhook(wid, s),
                description=f"update_callback_url({webhook_id})",
            )
            self._invalidate_list_cache()
            log.info(
                "[WEBHOOK:UPDATE_URL] Updated webhook_id=%s", webhook_id
            )
            return True
        except Exception as e:
            log.error(
                "[WEBHOOK:UPDATE_URL] Failed webhook_id=%s: %s",
                webhook_id,
                e,
            )
            return False

    # ----- delete / cleanup ----------------------------------------------

    async def delete_webhook(self, webhook_id: int) -> bool:
        """Delete a webhook. Returns True if deleted OR already gone."""
        try:
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id: c.Webhooks.delete_webhook(wid),
                description=f"delete_webhook({webhook_id})",
            )
            self._invalidate_list_cache()
            log.info("[WEBHOOK:DELETE] Deleted webhook_id=%s", webhook_id)
            return True
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "1006" in error_msg:
                log.warning(
                    "[WEBHOOK:DELETE] Webhook already gone webhook_id=%s",
                    webhook_id,
                )
                return True
            log.error(
                "[WEBHOOK:DELETE] Failed webhook_id=%s: %s", webhook_id, e
            )
            return False

    async def reset_webhook(self, webhook_id: int) -> bool:
        """Disable then re-enable a webhook (nudge bad states)."""
        try:
            disable_spec = smartsheet.models.Webhook({"enabled": False})
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id, s=disable_spec: c.Webhooks.update_webhook(
                    wid, s
                ),
                description=f"reset_disable({webhook_id})",
            )

            enable_spec = smartsheet.models.Webhook({"enabled": True})
            await self._client.call_with_retry_async(
                lambda c, wid=webhook_id, s=enable_spec: c.Webhooks.update_webhook(
                    wid, s
                ),
                description=f"reset_enable({webhook_id})",
            )
            self._invalidate_list_cache()
            log.info("[WEBHOOK:RESET] Reset webhook_id=%s", webhook_id)
            return True
        except Exception as e:
            log.error(
                "[WEBHOOK:RESET] Failed webhook_id=%s: %s", webhook_id, e
            )
            return False

    async def cleanup_all_webhooks_for_worksheet(
        self,
        worksheet_id: int,
        protected_ids: Optional[Set[int]] = None,
        ownership_patterns: Optional[List[str]] = None,
    ) -> int:
        """Delete webhooks bound to ``worksheet_id`` that this product owns."""
        protected_ids = protected_ids or set()
        ownership_patterns = [p for p in (ownership_patterns or []) if p]
        log.info(
            "[WEBHOOK:CLEANUP_ALL] Cleaning owned webhooks worksheet_id=%s",
            worksheet_id,
        )

        try:
            all_webhooks = await self.list_webhooks(max_age_s=30)

            worksheet_webhooks = []
            for w in all_webhooks:
                if w.scope_object_id != worksheet_id:
                    continue
                if w.id in protected_ids:
                    log.debug(
                        "[WEBHOOK:CLEANUP_ALL] Skipping webhook %s "
                        "(owned by another org)",
                        w.id,
                    )
                    continue
                if ownership_patterns and not is_our_webhook_callback(
                    w.callback_url or "", ownership_patterns
                ):
                    log.debug(
                        "[WEBHOOK:CLEANUP_ALL] Skipping webhook %s "
                        "(foreign callback %s)",
                        w.id,
                        w.callback_url,
                    )
                    continue
                worksheet_webhooks.append(w)

            if not worksheet_webhooks:
                log.info(
                    "[WEBHOOK:CLEANUP_ALL] No existing webhooks to clean up "
                    "worksheet_id=%s",
                    worksheet_id,
                )
                return 0

            deleted_count = 0
            for webhook in worksheet_webhooks:
                try:
                    if await self.delete_webhook(webhook.id):
                        deleted_count += 1
                except Exception as del_error:
                    log.warning(
                        "[WEBHOOK:CLEANUP_ALL] Could not delete webhook %s: %s",
                        webhook.id,
                        del_error,
                    )

            log.info(
                "[WEBHOOK:CLEANUP_ALL] Cleaned %s/%s webhooks worksheet_id=%s",
                deleted_count,
                len(worksheet_webhooks),
                worksheet_id,
            )
            return deleted_count

        except Exception as e:
            log.error(
                "[WEBHOOK:CLEANUP_ALL] Failed worksheet_id=%s: %s",
                worksheet_id,
                e,
            )
            return 0

    async def cleanup_duplicate_webhooks(
        self,
        worksheet_id: int,
        keep_webhook_id: Optional[int] = None,
        protected_ids: Optional[Set[int]] = None,
        ownership_patterns: Optional[List[str]] = None,
    ) -> int:
        """Delete duplicate webhooks for a worksheet, keeping one."""
        protected_ids = protected_ids or set()
        ownership_patterns = [p for p in (ownership_patterns or []) if p]
        log.info(
            "[WEBHOOK:CLEANUP] Cleaning duplicates worksheet_id=%s",
            worksheet_id,
        )

        webhooks = []
        for w in await self.get_webhooks_for_worksheet(worksheet_id):
            if w.id in protected_ids:
                continue
            if ownership_patterns and not is_our_webhook_callback(
                w.callback_url or "", ownership_patterns
            ):
                continue
            webhooks.append(w)

        if len(webhooks) <= 1:
            log.info(
                "[WEBHOOK:CLEANUP] No duplicates worksheet_id=%s", worksheet_id
            )
            return 0

        log.warning(
            "[WEBHOOK:CLEANUP] Found %s webhooks for worksheet_id=%s",
            len(webhooks),
            worksheet_id,
        )

        to_keep = None
        if keep_webhook_id:
            to_keep = next(
                (w for w in webhooks if w.id == keep_webhook_id), None
            )
        if not to_keep:
            healthy = [w for w in webhooks if w.is_healthy]
            to_keep = healthy[0] if healthy else webhooks[0]

        deleted = 0
        for webhook in webhooks:
            if webhook.id != to_keep.id:
                if await self.delete_webhook(webhook.id):
                    deleted += 1

        log.info(
            "[WEBHOOK:CLEANUP] Deleted %s duplicates, kept %s",
            deleted,
            to_keep.id,
        )
        return deleted

    def _invalidate_list_cache(self) -> None:
        """Drop the cached ``list_webhooks`` result."""
        self._list_cache = None
        self._list_cache_time = 0
