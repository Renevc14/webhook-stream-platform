"""SH-210 — gateway-owned Smartsheet webhook lifecycle (register / renew / zombie).

Completes the SH-173 secret-sync MVP: the gateway now also owns the Smartsheet
side of the webhook contract for the tools listed in
``WEBHOOK_REGISTRAR_TOOLS`` — registering missing webhooks, re-enabling
disabled ones, recreating definitive failures, fixing callback-URL drift, and
deleting zombies that no longer back an active config.

Ownership rules ported from I2C ``webhook_lifecycle.py`` (SH-178):

* Only webhooks whose ``callbackUrl`` matches this deployment's gateway prefix
  for the tool are ever touched — other tools on the same sheet are invisible.
* A webhook registered to a **different** organization is protected: never
  deleted, never counted as a duplicate.
* When the cross-org ownership lookup fails the cycle is **fail-closed**:
  nothing is deleted, a warning is logged, and the next cycle retries.

Cell History 2 is deliberately not represented here — it keeps its own
lifecycle until a separate ticket migrates it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from smarthub_core.crypto import CryptoService
from smarthub_core.platforms.smartsheet import (
    SmartsheetWebhook,
    gateway_callback_url,
    is_our_webhook_callback,
)

from gateway.lifecycle.platform_registrations import (
    delete_platform_registration,
    upsert_platform_registration,
)
from gateway.secrets.store import (
    DEFAULT_PROVIDER,
    DEFAULT_SECRETS_TTL_S,
    WebhookSecretStore,
)
from gateway.tools.registry import (
    ToolRegistry,
    ToolRegistryEntry,
    load_tool_registry,
)

log = logging.getLogger(__name__)

# A NEW_NOT_VERIFIED webhook younger than this is still waiting for Smartsheet's
# verification callback; recreating it early causes a register/delete loop.
MIN_AGE_FOR_REREGISTER_MINUTES = 10.0


@dataclass(frozen=True)
class WebhookToolSpec:
    """How one tool stores its configs and webhooks (no Cell History 2)."""

    tool_slug: str
    configs_table: str
    webhooks_table: str
    secret_column: str = "shared_secret"
    paused_column: str = "paused"
    processing_mode_column: Optional[str] = "processing_mode"
    processing_modes: Tuple[str, ...] = ("webhook",)
    runner_column: Optional[str] = "runner_user_id"
    callback_column: str = "callback_url"
    name_prefix: str = "Smarthub"
    # Namespaces the secret cache key (SH-215). This registrar only speaks
    # Smartsheet; another provider needs its own lifecycle, not just a slug.
    provider: str = DEFAULT_PROVIDER

    @classmethod
    def from_registry_entry(cls, entry: ToolRegistryEntry) -> "WebhookToolSpec":
        """Translate a control-plane registry entry for Smartsheet reconciliation."""
        return cls(
            tool_slug=entry.tool_slug,
            configs_table=entry.configs_table,
            webhooks_table=entry.webhooks_table,
            secret_column=entry.secret_column,
            paused_column=entry.paused_column,
            processing_mode_column=entry.processing_mode_column,
            processing_modes=entry.processing_modes,
            runner_column=entry.runner_column,
            callback_column=entry.callback_column or "callback_url",
            name_prefix=entry.name_prefix,
            provider=entry.provider,
        )

_DEFAULT_REGISTRY = load_tool_registry()
I2C_SPEC = WebhookToolSpec.from_registry_entry(
    _DEFAULT_REGISTRY.require("smart-image-to-cell")
)
SMARTQR_SPEC = WebhookToolSpec.from_registry_entry(_DEFAULT_REGISTRY.require("smart-qr"))
KNOWN_SPECS: Dict[str, WebhookToolSpec] = {
    entry.tool_slug: WebhookToolSpec.from_registry_entry(entry)
    for entry in _DEFAULT_REGISTRY.all_entries()
}


def specs_for(
    tool_slugs: Sequence[str], registry: Optional[ToolRegistry] = None
) -> List[WebhookToolSpec]:
    """Resolve configured slugs to specs, ignoring unknown ones with a warning."""
    reg = registry or _DEFAULT_REGISTRY
    return [
        WebhookToolSpec.from_registry_entry(entry)
        for entry in reg.registrar_entries(tool_slugs)
    ]


@dataclass
class RegistrarResult:
    configs_checked: int = 0
    registered: int = 0
    re_enabled: int = 0
    url_updated: int = 0
    waiting: int = 0
    healthy: int = 0
    zombies_deleted: int = 0
    unowned_skipped: int = 0
    errors: int = 0
    tools: List[str] = field(default_factory=list)

    def merge(self, other: "RegistrarResult") -> None:
        self.configs_checked += other.configs_checked
        self.registered += other.registered
        self.re_enabled += other.re_enabled
        self.url_updated += other.url_updated
        self.waiting += other.waiting
        self.healthy += other.healthy
        self.zombies_deleted += other.zombies_deleted
        self.unowned_skipped += other.unowned_skipped
        self.errors += other.errors
        for tool in other.tools:
            if tool not in self.tools:
                self.tools.append(tool)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "configs_checked": self.configs_checked,
            "registered": self.registered,
            "re_enabled": self.re_enabled,
            "url_updated": self.url_updated,
            "waiting": self.waiting,
            "healthy": self.healthy,
            "zombies_deleted": self.zombies_deleted,
            "unowned_skipped": self.unowned_skipped,
            "errors": self.errors,
            "tools": list(self.tools),
        }


# Factory signature: (org_id, tool_slug, runner_user_id) -> SmartsheetWebhookService
WebhookServiceFactory = Callable[[str, str, Optional[str]], Awaitable[Any]]


class WebhookRegistrar:
    """Reconciles Smartsheet webhooks with active tool configs, per org."""

    def __init__(
        self,
        *,
        supabase,
        secret_store: WebhookSecretStore,
        crypto: CryptoService,
        webhook_service_factory: WebhookServiceFactory,
        callback_base_url: str,
        secrets_ttl_s: int = DEFAULT_SECRETS_TTL_S,
        min_age_minutes: float = MIN_AGE_FOR_REREGISTER_MINUTES,
        platform_dual_write: bool = True,
    ) -> None:
        if not callback_base_url:
            raise ValueError("callback_base_url is required for the registrar")
        self._sb = supabase
        self._store = secret_store
        self._crypto = crypto
        self._factory = webhook_service_factory
        self._base = callback_base_url.rstrip("/")
        self._ttl_s = secrets_ttl_s
        self._min_age_minutes = min_age_minutes
        self._platform_dual_write = platform_dual_write

    # ----- public API -----------------------------------------------------

    async def reconcile_all(self, specs: Sequence[WebhookToolSpec]) -> RegistrarResult:
        total = RegistrarResult()
        for spec in specs:
            total.tools.append(spec.tool_slug)
            try:
                configs = self._fetch_active_configs(spec)
            except Exception as exc:
                log.error(
                    "webhook_gateway: registrar failed to load configs tool=%s: %s",
                    spec.tool_slug,
                    exc,
                )
                total.errors += 1
                continue

            for org_id, org_configs in _group_by_org(configs).items():
                total.merge(await self.reconcile_org(spec, org_id, org_configs))

        log.info("webhook_gateway: registrar cycle done %s", total.as_dict())
        return total

    async def reconcile_org(
        self,
        spec: WebhookToolSpec,
        org_id: str,
        configs: List[Dict[str, Any]],
    ) -> RegistrarResult:
        out = RegistrarResult(configs_checked=len(configs))
        callback_url = gateway_callback_url(
            self._base, spec.tool_slug, org_id, provider=spec.provider
        )
        patterns = [f"{self._base}/webhook/{spec.provider}/{spec.tool_slug}/"]

        # Fail-closed: without the cross-org ownership map we cannot prove a
        # webhook is ours to delete, so the destructive steps are skipped.
        protected_ids, ownership_known = self._protected_webhook_ids(spec, org_id)

        try:
            service = await self._factory(
                org_id, spec.tool_slug, _runner_of(configs, spec)
            )
        except Exception as exc:
            log.error(
                "webhook_gateway: registrar cannot build Smartsheet service "
                "tool=%s org=%s: %s",
                spec.tool_slug,
                org_id,
                exc,
            )
            out.errors += 1
            return out

        try:
            all_webhooks = await service.list_webhooks()
        except Exception as exc:
            log.error(
                "webhook_gateway: registrar list_webhooks failed tool=%s org=%s: %s",
                spec.tool_slug,
                org_id,
                exc,
            )
            out.errors += 1
            return out

        ours: List[SmartsheetWebhook] = []
        for webhook in all_webhooks:
            if not is_our_webhook_callback(webhook.callback_url or "", patterns):
                continue
            if webhook.id in protected_ids:
                out.unowned_skipped += 1
                continue
            ours.append(webhook)

        expected_sheets = {
            int(cfg["worksheet_id"]) for cfg in configs if cfg.get("worksheet_id")
        }
        secrets: Dict[str, str] = {}

        for cfg in configs:
            worksheet_id = cfg.get("worksheet_id")
            if not worksheet_id:
                out.errors += 1
                continue
            sheet_webhooks = [
                w for w in ours if int(w.scope_object_id or 0) == int(worksheet_id)
            ]
            try:
                status, webhook = await self._reconcile_config(
                    spec=spec,
                    service=service,
                    org_id=org_id,
                    config=cfg,
                    callback_url=callback_url,
                    patterns=patterns,
                    protected_ids=protected_ids,
                    ownership_known=ownership_known,
                    sheet_webhooks=sheet_webhooks,
                )
            except Exception as exc:
                log.error(
                    "webhook_gateway: registrar failed tool=%s org=%s sheet=%s: %s",
                    spec.tool_slug,
                    org_id,
                    worksheet_id,
                    exc,
                )
                out.errors += 1
                continue

            _count(out, status)
            if webhook is not None and webhook.shared_secret:
                secrets[str(webhook.id)] = webhook.shared_secret

        if ownership_known:
            out.zombies_deleted += await self._delete_zombies(
                spec=spec,
                service=service,
                org_id=org_id,
                ours=ours,
                expected_sheets=expected_sheets,
            )
        else:
            log.warning(
                "webhook_gateway: registrar skipping zombie cleanup tool=%s org=%s "
                "(cross-org ownership unknown — fail closed)",
                spec.tool_slug,
                org_id,
            )

        if secrets:
            await self._merge_secrets(spec, org_id, secrets)

        return out

    # ----- per-config state machine ---------------------------------------

    async def _reconcile_config(
        self,
        *,
        spec: WebhookToolSpec,
        service,
        org_id: str,
        config: Dict[str, Any],
        callback_url: str,
        patterns: List[str],
        protected_ids: Set[int],
        ownership_known: bool,
        sheet_webhooks: List[SmartsheetWebhook],
    ) -> Tuple[str, Optional[SmartsheetWebhook]]:
        worksheet_id = int(config["worksheet_id"])

        if not sheet_webhooks:
            return await self._register(
                spec=spec,
                service=service,
                org_id=org_id,
                config=config,
                callback_url=callback_url,
                patterns=patterns,
                protected_ids=protected_ids,
                ownership_known=ownership_known,
            )

        if len(sheet_webhooks) > 1:
            keep = _pick_survivor(sheet_webhooks)
            for extra in sheet_webhooks:
                if extra.id == keep.id or extra.id in protected_ids:
                    continue
                if await service.delete_webhook(extra.id):
                    self._delete_webhook_row(spec, extra.id)
            sheet_webhooks = [keep]

        webhook = sheet_webhooks[0]

        if webhook.is_disabled:
            if webhook.is_definitive_failure:
                log.warning(
                    "webhook_gateway: registrar recreating webhook=%s status=%s",
                    webhook.id,
                    webhook.status,
                )
                await service.delete_webhook(webhook.id)
                self._delete_webhook_row(spec, webhook.id)
                return await self._register(
                    spec=spec,
                    service=service,
                    org_id=org_id,
                    config=config,
                    callback_url=callback_url,
                    patterns=patterns,
                    protected_ids=protected_ids,
                    ownership_known=ownership_known,
                )
            if await service.enable_webhook(webhook.id):
                self._upsert_webhook_row(
                    spec, org_id, worksheet_id, webhook, config, status="active"
                )
                return "re_enabled", webhook
            return "error", None

        if webhook.needs_verification:
            age = webhook.age_minutes
            if age is not None and age < self._min_age_minutes:
                return "waiting", webhook
            log.warning(
                "webhook_gateway: registrar webhook=%s unverified for %s min — recreating",
                webhook.id,
                f"{age:.1f}" if age is not None else "unknown",
            )
            await service.delete_webhook(webhook.id)
            self._delete_webhook_row(spec, webhook.id)
            return await self._register(
                spec=spec,
                service=service,
                org_id=org_id,
                config=config,
                callback_url=callback_url,
                patterns=patterns,
                protected_ids=protected_ids,
                ownership_known=ownership_known,
            )

        if webhook.callback_url != callback_url:
            log.warning(
                "webhook_gateway: registrar URL drift webhook=%s %s != %s",
                webhook.id,
                webhook.callback_url,
                callback_url,
            )
            if await service.update_callback_url(webhook.id, callback_url):
                self._upsert_webhook_row(
                    spec,
                    org_id,
                    worksheet_id,
                    webhook,
                    config,
                    status="active",
                    callback_url=callback_url,
                )
                return "url_updated", webhook
            return "error", None

        if webhook.is_healthy:
            self._upsert_webhook_row(
                spec, org_id, worksheet_id, webhook, config, status="active"
            )
            return "healthy", webhook

        return "error", None

    async def _register(
        self,
        *,
        spec: WebhookToolSpec,
        service,
        org_id: str,
        config: Dict[str, Any],
        callback_url: str,
        patterns: List[str],
        protected_ids: Set[int],
        ownership_known: bool,
    ) -> Tuple[str, Optional[SmartsheetWebhook]]:
        worksheet_id = int(config["worksheet_id"])
        name = f"{spec.name_prefix} - {config.get('configuration_name') or config.get('name') or worksheet_id}"

        webhook = await service.register_webhook(
            worksheet_id=worksheet_id,
            callback_url=callback_url,
            name=name,
            # Destructive pre-cleanup only runs when ownership is provable.
            cleanup_existing=ownership_known,
            protected_ids=protected_ids,
            ownership_patterns=patterns,
        )
        self._upsert_webhook_row(
            spec,
            org_id,
            worksheet_id,
            webhook,
            config,
            status="active",
            callback_url=callback_url,
        )
        log.info(
            "webhook_gateway: registrar registered webhook=%s tool=%s org=%s sheet=%s",
            webhook.id,
            spec.tool_slug,
            org_id,
            worksheet_id,
        )
        return "registered", webhook

    async def _delete_zombies(
        self,
        *,
        spec: WebhookToolSpec,
        service,
        org_id: str,
        ours: List[SmartsheetWebhook],
        expected_sheets: Set[int],
    ) -> int:
        deleted = 0
        for webhook in ours:
            sheet_id = int(webhook.scope_object_id or 0)
            if sheet_id in expected_sheets:
                continue
            log.warning(
                "webhook_gateway: registrar deleting zombie webhook=%s sheet=%s "
                "tool=%s org=%s (no active config)",
                webhook.id,
                sheet_id,
                spec.tool_slug,
                org_id,
            )
            if await service.delete_webhook(webhook.id):
                self._delete_webhook_row(spec, webhook.id)
                deleted += 1
        return deleted

    # ----- Supabase helpers -----------------------------------------------

    def _fetch_active_configs(self, spec: WebhookToolSpec) -> List[Dict[str, Any]]:
        def _query():
            q = self._sb.table(spec.configs_table).select("*").eq(spec.paused_column, False)
            if spec.processing_mode_column:
                q = q.in_(spec.processing_mode_column, list(spec.processing_modes))
            return q.execute()

        result = self._sb.execute_with_retry(
            _query, f"list active configs {spec.configs_table}"
        )
        rows = list(result.data or [])
        # Configs parked because their runner lost OAuth cannot be registered.
        return [
            r
            for r in rows
            if (r.get("connection_status") or "active") != "needs_connection"
        ]

    def _protected_webhook_ids(
        self, spec: WebhookToolSpec, org_id: str
    ) -> Tuple[Set[int], bool]:
        """Webhook ids this org does NOT own. Second value is False when the
        lookup failed (caller must then skip every destructive step)."""
        try:
            result = self._sb.execute_with_retry(
                lambda: self._sb.table(spec.webhooks_table)
                .select("organization_id,webhook_id")
                .execute(),
                f"list webhook owners {spec.webhooks_table}",
            )
        except Exception as exc:
            log.error(
                "webhook_gateway: registrar ownership lookup failed tool=%s: %s",
                spec.tool_slug,
                exc,
            )
            return set(), False

        protected: Set[int] = set()
        for row in list(result.data or []):
            webhook_id = row.get("webhook_id")
            if not webhook_id:
                continue
            if str(row.get("organization_id")) != str(org_id):
                protected.add(int(webhook_id))
        return protected, True

    def _existing_row_id(
        self, spec: WebhookToolSpec, org_id: str, worksheet_id: int
    ) -> Optional[Any]:
        try:
            result = self._sb.execute_with_retry(
                lambda: self._sb.table(spec.webhooks_table)
                .select("id,webhook_id")
                .eq("organization_id", org_id)
                .eq("worksheet_id", worksheet_id)
                .execute(),
                f"find webhook row {spec.webhooks_table}",
            )
        except Exception as exc:
            log.warning(
                "webhook_gateway: registrar row lookup failed tool=%s: %s",
                spec.tool_slug,
                exc,
            )
            return None
        rows = list(result.data or [])
        return rows[0].get("id") if rows else None

    def _upsert_webhook_row(
        self,
        spec: WebhookToolSpec,
        org_id: str,
        worksheet_id: int,
        webhook: SmartsheetWebhook,
        config: Dict[str, Any],
        *,
        status: str = "active",
        callback_url: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "organization_id": org_id,
            "worksheet_id": worksheet_id,
            "webhook_id": webhook.id,
            "status": status,
            "updated_at": now,
        }
        runner = _runner_from_config(config, spec)
        # Only write runner when the tool table actually has that column.
        if runner and spec.runner_column:
            payload[spec.runner_column] = runner
        if callback_url and spec.callback_column:
            payload[spec.callback_column] = callback_url
        if webhook.shared_secret:
            # Ciphertext in Postgres; plaintext only ever reaches the Redis cache.
            payload[spec.secret_column] = self._crypto.encrypt(webhook.shared_secret)

        # SH-230 D: platform_webhook_registrations is write SoT (required).
        # Tool *_webhooks remain a soft-fail mirror for rollback until drop.
        cipher = payload.get(spec.secret_column)
        config_id = config.get("id") or config.get("configuration_id")
        try:
            upsert_platform_registration(
                self._sb,
                provider=spec.provider,
                tool_slug=spec.tool_slug,
                organization_id=org_id,
                worksheet_id=worksheet_id,
                webhook_id=int(webhook.id),
                shared_secret_ciphertext=cipher,
                callback_url=callback_url
                or payload.get(spec.callback_column),
                status=status,
                runner_user_id=str(runner) if runner else None,
                config_id=str(config_id) if config_id else None,
                soft_fail=False,
            )
        except Exception as exc:
            log.error(
                "webhook_gateway: registrar platform persist failed webhook=%s "
                "tool=%s: %s",
                webhook.id,
                spec.tool_slug,
                exc,
            )
            return

        if not self._platform_dual_write:
            return

        row_id = self._existing_row_id(spec, org_id, worksheet_id)
        try:
            if row_id is not None:
                self._sb.execute_with_retry(
                    lambda: self._sb.table(spec.webhooks_table)
                    .update(payload)
                    .eq("id", row_id)
                    .execute(),
                    f"update webhook row {spec.webhooks_table}",
                )
            else:
                payload["created_at"] = now
                self._sb.execute_with_retry(
                    lambda: self._sb.table(spec.webhooks_table)
                    .insert(payload)
                    .execute(),
                    f"insert webhook row {spec.webhooks_table}",
                )
        except Exception as exc:
            log.warning(
                "webhook_gateway: registrar tool-table mirror soft-fail "
                "webhook=%s tool=%s: %s",
                webhook.id,
                spec.tool_slug,
                exc,
            )

    def _delete_webhook_row(self, spec: WebhookToolSpec, webhook_id: int) -> None:
        try:
            delete_platform_registration(
                self._sb,
                provider=spec.provider,
                tool_slug=spec.tool_slug,
                webhook_id=int(webhook_id),
                soft_fail=False,
            )
        except Exception as exc:
            log.warning(
                "webhook_gateway: registrar platform delete failed webhook=%s: %s",
                webhook_id,
                exc,
            )
            return

        if not self._platform_dual_write:
            return

        try:
            self._sb.execute_with_retry(
                lambda: self._sb.table(spec.webhooks_table)
                .delete()
                .eq("webhook_id", webhook_id)
                .execute(),
                f"delete webhook row {spec.webhooks_table}",
            )
        except Exception as exc:
            log.warning(
                "webhook_gateway: registrar tool-table delete soft-fail "
                "webhook=%s: %s",
                webhook_id,
                exc,
            )

    async def _merge_secrets(
        self, spec: WebhookToolSpec, org_id: str, secrets: Dict[str, str]
    ) -> None:
        """Fold freshly captured secrets into the org's cached map.

        Smartsheet only returns ``sharedSecret`` on create/update, so a secret
        captured here would otherwise be lost until the next pull-sync.
        """
        try:
            current = await self._store.get_secrets(
                org_id, spec.tool_slug, provider=spec.provider
            )
        except Exception:
            current = {}
        current.update(secrets)
        await self._store.put_secrets(
            org_id,
            spec.tool_slug,
            current,
            provider=spec.provider,
            ttl_s=self._ttl_s,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_by_org(configs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for cfg in configs:
        org_id = cfg.get("organization_id")
        if not org_id:
            continue
        grouped.setdefault(str(org_id), []).append(cfg)
    return grouped


def _runner_from_config(
    config: Dict[str, Any], spec: WebhookToolSpec
) -> Optional[str]:
    if spec.runner_column:
        runner = config.get(spec.runner_column)
        if runner:
            return str(runner)
    created_by = config.get("created_by")
    return str(created_by) if created_by else None


def _runner_of(configs: List[Dict[str, Any]], spec: WebhookToolSpec) -> Optional[str]:
    for cfg in configs:
        runner = _runner_from_config(cfg, spec)
        if runner:
            return runner
    return None


def _pick_survivor(webhooks: List[SmartsheetWebhook]) -> SmartsheetWebhook:
    healthy = [w for w in webhooks if w.is_healthy]
    return healthy[0] if healthy else webhooks[0]


def _count(result: RegistrarResult, status: str) -> None:
    if status == "registered":
        result.registered += 1
    elif status == "re_enabled":
        result.re_enabled += 1
    elif status == "url_updated":
        result.url_updated += 1
    elif status == "waiting":
        result.waiting += 1
    elif status == "healthy":
        result.healthy += 1
    else:
        result.errors += 1
