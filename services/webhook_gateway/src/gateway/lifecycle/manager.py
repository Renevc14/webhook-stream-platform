"""SH-173 / SH-210 — gateway lifecycle manager (Redis singleton + webhooks).

Cycle:

1. Acquire ``lock:lifecycle:manager`` (TTL 600s / heartbeat 60s).
2. Refresh ``webhook-secrets:{org}:{provider}:{tool}`` from each tool's webhook table
   (SH-173 / SH-208 multi-tool pull sync).
3. SH-210 — when ``RUN_WEBHOOK_REGISTRAR=true``, reconcile Smartsheet webhooks
   for the tools in ``WEBHOOK_REGISTRAR_TOOLS``: register missing, re-enable
   disabled, fix callback-URL drift, delete zombies.
4. Release lock; sleep ``WEBHOOK_LIFECYCLE_INTERVAL_S`` (default 300).

A tool must set its own ``RUN_LIFECYCLE_POLLER=false`` before being added to
``WEBHOOK_REGISTRAR_TOOLS`` — two owners on the same Smartsheet webhook would
delete each other's registrations. Cell History 2 is never registered here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

from gateway.config.settings import Settings, get_settings
from gateway.lifecycle.lock import hold_lifecycle_lock
from gateway.lifecycle.registrar import (
    WebhookRegistrar,
    WebhookToolSpec,
    specs_for,
)
from gateway.lifecycle.secret_sync import SecretCacheSync, default_secret_sources
from gateway.secrets.store import WebhookSecretStore

log = logging.getLogger(__name__)

_MANAGER_TASK: Optional[asyncio.Task] = None
_ACTIVE_MANAGER: Optional["LifecycleManager"] = None


def get_active_lifecycle_manager() -> Optional["LifecycleManager"]:
    """Return the running manager (for m2m ensure-registration), if any."""
    return _ACTIVE_MANAGER


class LifecycleManager:
    """Runs secret-cache sync under the Redis singleton lock."""

    def __init__(
        self,
        *,
        redis_client,
        secret_store: WebhookSecretStore,
        secret_sync: SecretCacheSync,
        settings: Optional[Settings] = None,
        registrar: Optional[WebhookRegistrar] = None,
        registrar_specs: Optional[Sequence[WebhookToolSpec]] = None,
    ) -> None:
        self._redis = redis_client
        self._store = secret_store
        self._sync = secret_sync
        self._settings = settings or get_settings()
        self._registrar = registrar
        self._registrar_specs: List[WebhookToolSpec] = list(registrar_specs or [])
        self._stop = asyncio.Event()

    def request_shutdown(self) -> None:
        self._stop.set()

    async def run_once(self) -> dict:
        """Run a single locked cycle (used by tests and the supervisor)."""
        async with hold_lifecycle_lock(
            self._redis,
            ttl_s=self._settings.lifecycle_lock_ttl_s,
            heartbeat_interval_s=self._settings.lifecycle_lock_heartbeat_s,
        ) as acquired:
            if not acquired:
                return {"skipped": "lock_busy"}
            summary: Dict[str, Any] = (await self._sync.refresh_all()).as_dict()
            summary["registrar"] = await self._run_registrar()
            return summary

    async def _run_registrar(self) -> Dict[str, Any]:
        if self._registrar is None or not self._registrar_specs:
            return {"skipped": "disabled"}
        try:
            result = await self._registrar.reconcile_all(self._registrar_specs)
        except Exception:
            # Never let a registrar failure kill the cycle — the secret cache
            # the ingress path depends on has already been refreshed.
            log.exception("webhook_gateway: registrar cycle crashed")
            return {"error": "registrar_crashed"}
        return result.as_dict()

    async def ensure_registration(
        self,
        *,
        tool_slug: str,
        org_id: str,
        worksheet_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """On-demand registrar reconcile for one org (optional sheet filter).

        Used when a tool has handed webhook ownership to the gateway (Pulse
        cutover) and needs the webhook registered before the next poll cycle.
        Merges any freshly captured shared secrets into Redis immediately.
        """
        if self._registrar is None or not self._registrar_specs:
            return {"ok": False, "error": "registrar_disabled"}

        slug = (tool_slug or "").strip()
        specs = [s for s in self._registrar_specs if s.tool_slug == slug]
        if not specs:
            return {
                "ok": False,
                "error": "tool_not_in_registrar",
                "tool_slug": slug,
            }

        spec = specs[0]
        try:
            configs = self._registrar._fetch_active_configs(spec)
        except Exception as exc:
            log.exception(
                "webhook_gateway: ensure_registration config load failed tool=%s",
                slug,
            )
            return {"ok": False, "error": f"config_load_failed: {exc}"}

        org_configs = [
            c
            for c in configs
            if str(c.get("organization_id") or "") == str(org_id)
        ]
        if worksheet_id is not None:
            org_configs = [
                c
                for c in org_configs
                if int(c.get("worksheet_id") or 0) == int(worksheet_id)
            ]

        if not org_configs:
            return {
                "ok": False,
                "error": "no_active_configs",
                "tool_slug": slug,
                "org_id": org_id,
                "worksheet_id": worksheet_id,
            }

        try:
            result = await self._registrar.reconcile_org(
                spec, str(org_id), org_configs
            )
        except Exception as exc:
            log.exception(
                "webhook_gateway: ensure_registration reconcile failed "
                "tool=%s org=%s",
                slug,
                org_id,
            )
            return {"ok": False, "error": f"reconcile_failed: {exc}"}

        summary = result.as_dict()
        summary["ok"] = result.errors == 0
        summary["tool_slug"] = slug
        summary["org_id"] = org_id
        log.info("webhook_gateway: ensure_registration done %s", summary)
        return summary

    async def run(self) -> None:
        interval = max(5, int(self._settings.webhook_lifecycle_interval_s))
        log.info(
            "webhook_gateway: lifecycle manager starting interval=%ds lock_ttl=%ds",
            interval,
            self._settings.lifecycle_lock_ttl_s,
        )
        while not self._stop.is_set():
            try:
                summary = await self.run_once()
                log.info("webhook_gateway: lifecycle cycle done %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook_gateway: lifecycle cycle crashed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        log.info("webhook_gateway: lifecycle manager stopped")


def _build_secret_sync(
    *,
    settings: Settings,
    secret_store: WebhookSecretStore,
) -> Optional[SecretCacheSync]:
    if not settings.supabase_url or not settings.supabase_service_role_key:
        log.warning(
            "webhook_gateway: SUPABASE_URL/KEY missing — lifecycle secret sync disabled"
        )
        return None
    if not settings.app_crypto_key:
        log.warning(
            "webhook_gateway: APP_CRYPTO_KEY missing — lifecycle secret sync disabled"
        )
        return None

    from smarthub_core.crypto import CryptoService
    from smarthub_core.db.supabase import SupabaseService

    sb = SupabaseService(settings.supabase_url, settings.supabase_service_role_key)
    crypto = CryptoService(settings.app_crypto_key)
    sources = default_secret_sources(
        registry=settings.tool_registry(),
        prefer_platform_table=settings.platform_webhook_read,
    )
    log.info(
        "webhook_gateway: secret sync sources=%s platform_read=%s",
        [(s.tool_slug, s.table) for s in sources],
        settings.platform_webhook_read,
    )
    return SecretCacheSync(
        supabase=sb,
        secret_store=secret_store,
        crypto=crypto,
        sources=sources,
        secrets_ttl_s=settings.webhook_secrets_ttl_s,
    )


def build_registrar(
    *,
    settings: Settings,
    secret_store: WebhookSecretStore,
    redis_client,
) -> tuple[Optional[WebhookRegistrar], List[WebhookToolSpec]]:
    """Build the SH-210 registrar, or ``(None, [])`` when it must stay off."""
    if not settings.run_webhook_registrar:
        return None, []

    base_url = (settings.smartsheet_webhook_callback_base_url or "").strip()
    if not base_url:
        log.warning(
            "webhook_gateway: RUN_WEBHOOK_REGISTRAR=true but "
            "SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL missing — registrar disabled"
        )
        return None, []
    if redis_client is None:
        log.warning("webhook_gateway: no Redis — registrar disabled")
        return None, []
    if not settings.supabase_url or not settings.supabase_service_role_key:
        log.warning("webhook_gateway: SUPABASE_URL/KEY missing — registrar disabled")
        return None, []
    if not settings.app_crypto_key:
        log.warning("webhook_gateway: APP_CRYPTO_KEY missing — registrar disabled")
        return None, []

    specs = specs_for(
        settings.webhook_registrar_tools_list, registry=settings.tool_registry()
    )
    if not specs:
        log.warning(
            "webhook_gateway: WEBHOOK_REGISTRAR_TOOLS resolved to no known tool "
            "— registrar disabled"
        )
        return None, []

    from smarthub_core.crypto import CryptoService
    from smarthub_core.db.supabase import SupabaseService
    from smarthub_core.platforms.smartsheet import (
        SmartsheetClient,
        SmartsheetWebhookService,
    )

    sb = SupabaseService(settings.supabase_url, settings.supabase_service_role_key)
    crypto = CryptoService(settings.app_crypto_key)

    # SmartsheetClient token_resolver reads broker env vars from the process
    # environment. Mirror settings so ECS secrets / task-def env reach it.
    import os

    if settings.encrypt_tool_internal_url and not os.getenv("ENCRYPT_TOOL_INTERNAL_URL"):
        os.environ["ENCRYPT_TOOL_INTERNAL_URL"] = settings.encrypt_tool_internal_url
    if settings.smartsheet_broker_secret and not os.getenv("SMARTSHEET_BROKER_SECRET"):
        os.environ["SMARTSHEET_BROKER_SECRET"] = settings.smartsheet_broker_secret
    if settings.encrypt_tool_internal_url and settings.smartsheet_broker_secret:
        log.info(
            "webhook_gateway: OAuth broker configured for registrar "
            "(ENCRYPT_TOOL_INTERNAL_URL set)"
        )
    else:
        log.warning(
            "webhook_gateway: OAuth broker env incomplete — OAuth-only orgs "
            "will fail token resolve (set ENCRYPT_TOOL_INTERNAL_URL + "
            "SMARTSHEET_BROKER_SECRET)"
        )

    async def _factory(org_id: str, tool_slug: str, runner_user_id: Optional[str]):
        client = SmartsheetClient(
            org_id,
            tool_slug,
            redis=redis_client,
            supabase=sb,
            owner_user_id=runner_user_id,
        )
        return SmartsheetWebhookService(client)

    log.info(
        "webhook_gateway: registrar enabled tools=%s base=%s",
        [s.tool_slug for s in specs],
        base_url,
    )
    registrar = WebhookRegistrar(
        supabase=sb,
        secret_store=secret_store,
        crypto=crypto,
        webhook_service_factory=_factory,
        callback_base_url=base_url,
        secrets_ttl_s=settings.webhook_secrets_ttl_s,
        min_age_minutes=settings.webhook_registrar_min_age_minutes,
        platform_dual_write=settings.platform_webhook_dual_write,
    )
    return registrar, specs


async def start_lifecycle(
    *,
    redis_client,
    secret_store: WebhookSecretStore,
    settings: Optional[Settings] = None,
) -> Optional[asyncio.Task]:
    """Start the background lifecycle task. Returns None when disabled."""
    global _MANAGER_TASK
    cfg = settings or get_settings()
    if not cfg.run_lifecycle_poller:
        log.info("webhook_gateway: RUN_LIFECYCLE_POLLER=false — manager not started")
        return None
    if redis_client is None:
        log.warning("webhook_gateway: no Redis — lifecycle manager not started")
        return None

    sync = _build_secret_sync(settings=cfg, secret_store=secret_store)
    if sync is None:
        return None

    if _MANAGER_TASK is not None and not _MANAGER_TASK.done():
        return _MANAGER_TASK

    registrar, specs = build_registrar(
        settings=cfg, secret_store=secret_store, redis_client=redis_client
    )

    global _ACTIVE_MANAGER
    manager = LifecycleManager(
        redis_client=redis_client,
        secret_store=secret_store,
        secret_sync=sync,
        settings=cfg,
        registrar=registrar,
        registrar_specs=specs,
    )
    _ACTIVE_MANAGER = manager

    async def _runner() -> None:
        try:
            await manager.run()
        except asyncio.CancelledError:
            manager.request_shutdown()
            raise

    _MANAGER_TASK = asyncio.create_task(_runner(), name="gateway-lifecycle")
    _MANAGER_TASK._lifecycle_manager = manager  # type: ignore[attr-defined]
    return _MANAGER_TASK


async def stop_lifecycle() -> None:
    global _MANAGER_TASK, _ACTIVE_MANAGER
    if _MANAGER_TASK is None:
        _ACTIVE_MANAGER = None
        return
    manager = getattr(_MANAGER_TASK, "_lifecycle_manager", None)
    if manager is not None:
        manager.request_shutdown()
    _MANAGER_TASK.cancel()
    try:
        await _MANAGER_TASK
    except asyncio.CancelledError:
        pass
    finally:
        _MANAGER_TASK = None
        _ACTIVE_MANAGER = None
