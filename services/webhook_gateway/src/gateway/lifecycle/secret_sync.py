"""SH-173 / SH-208 / SH-228 — populate Redis webhook-secrets from Supabase.

Reads configured webhook tables from the tool registry (I2C + SmartQR by
default; overlays via GATEWAY_TOOL_REGISTRY_JSON), decrypts shared secrets with
``APP_CRYPTO_KEY`` (AES-GCM), and writes
``webhook-secrets:{org}:{provider}:{tool}`` as a plaintext
``{webhook_id: secret}`` map.
Ciphertext never leaves Postgres for long-term storage; Redis is a short-TTL
verification cache.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence

from smarthub_core.crypto import CryptoError, CryptoService

from gateway.secrets.store import (
    DEFAULT_PROVIDER,
    DEFAULT_SECRETS_TTL_S,
    WebhookSecretStore,
)
from gateway.tools.registry import ToolRegistry, ToolRegistryEntry, load_tool_registry

log = logging.getLogger(__name__)

# Backward-compatible constants (tests / older call sites).
I2C_WEBHOOKS_TABLE = "imagetocell_webhooks"
SMARTQR_WEBHOOKS_TABLE = "smartqr_webhooks"
DEFAULT_TOOL_SLUG = "smart-image-to-cell"
DEFAULT_SMARTQR_TOOL_SLUG = "smart-qr"


@dataclass(frozen=True)
class WebhookSecretSource:
    """One tool's webhook-secret table for gateway pull-sync (SH-208 / SH-228).

    ``provider`` namespaces the Redis cache key (SH-215) and is declared per
    source rather than inferred, so a tool that later adds a second provider
    gets its own table entry instead of silently sharing one secret map.

    SH-230 Phase C: when ``prefer_platform_table`` is true the sync reads
    ``platform_webhook_registrations`` (canonical ``shared_secret``) filtered
    by ``provider`` + ``tool_slug``, instead of the per-tool ``*_webhooks``
    table. Dual-write still targets both; tool tables remain write SoT.
    """

    tool_slug: str
    table: str
    secret_column: str = "shared_secret"
    status_value: str = "active"
    provider: str = DEFAULT_PROVIDER
    prefer_platform_table: bool = False

    @classmethod
    def from_registry_entry(
        cls,
        entry: ToolRegistryEntry,
        *,
        prefer_platform_table: bool = False,
    ) -> "WebhookSecretSource":
        if prefer_platform_table:
            from gateway.lifecycle.platform_registrations import (
                PLATFORM_WEBHOOKS_TABLE,
            )

            return cls(
                tool_slug=entry.tool_slug,
                table=PLATFORM_WEBHOOKS_TABLE,
                secret_column="shared_secret",
                provider=entry.provider,
                prefer_platform_table=True,
            )
        return cls(
            tool_slug=entry.tool_slug,
            table=entry.webhooks_table,
            secret_column=entry.secret_column,
            provider=entry.provider,
            prefer_platform_table=False,
        )


def default_secret_sources(
    *,
    i2c_tool_slug: str = DEFAULT_TOOL_SLUG,
    smartqr_tool_slug: str = DEFAULT_SMARTQR_TOOL_SLUG,
    smartqr_enabled: bool = True,
    registry: Optional[ToolRegistry] = None,
    prefer_platform_table: bool = False,
) -> List[WebhookSecretSource]:
    """Secret-sync sources from the tool registry (SH-228 / SH-230 Phase C).

    Legacy kwargs still remap builtin I2C/SmartQR slugs when ``registry`` is
    omitted. Cell History 2 stays out unless an overlay adds it.
    """
    reg = registry or load_tool_registry(
        i2c_tool_slug=i2c_tool_slug,
        smartqr_tool_slug=smartqr_tool_slug,
        smartqr_secret_sync_enabled=smartqr_enabled,
    )
    return [
        WebhookSecretSource.from_registry_entry(
            e, prefer_platform_table=prefer_platform_table
        )
        for e in reg.secret_sync_entries()
    ]


class _SupabaseLike(Protocol):
    def table(self, name: str): ...

    def execute_with_retry(self, operation, description: str = "operation", **kwargs): ...


class SecretSyncResult:
    def __init__(self) -> None:
        self.orgs_synced = 0
        self.secrets_cached = 0
        self.rows_skipped = 0
        self.errors = 0

    def merge(self, other: "SecretSyncResult") -> None:
        self.orgs_synced += other.orgs_synced
        self.secrets_cached += other.secrets_cached
        self.rows_skipped += other.rows_skipped
        self.errors += other.errors

    def as_dict(self) -> Dict[str, int]:
        return {
            "orgs_synced": self.orgs_synced,
            "secrets_cached": self.secrets_cached,
            "rows_skipped": self.rows_skipped,
            "errors": self.errors,
        }


def _decrypt_secret(crypto: CryptoService, raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        return crypto.decrypt(raw)
    except CryptoError:
        # Heuristic: AES-GCM wire format is base64(nonce||ct); short strings are
        # almost certainly plaintext leftovers from pre-SH-108 rows.
        if len(raw) < 40 and " " not in raw:
            return raw
        raise


class SecretCacheSync:
    """Load active webhook secrets from one or more tool tables into Redis."""

    def __init__(
        self,
        *,
        supabase: _SupabaseLike,
        secret_store: WebhookSecretStore,
        crypto: CryptoService,
        tool_slug: str = DEFAULT_TOOL_SLUG,
        secrets_ttl_s: int = DEFAULT_SECRETS_TTL_S,
        sources: Optional[Sequence[WebhookSecretSource]] = None,
    ) -> None:
        self._sb = supabase
        self._store = secret_store
        self._crypto = crypto
        self._ttl_s = secrets_ttl_s
        # Backward-compatible single-source constructor (SH-173 tests).
        if sources is None:
            sources = [
                WebhookSecretSource(
                    tool_slug=tool_slug,
                    table=I2C_WEBHOOKS_TABLE,
                    secret_column="shared_secret",
                )
            ]
        self._sources: List[WebhookSecretSource] = list(sources)

    def _fetch_active_rows(self, source: WebhookSecretSource) -> list:
        cols = f"organization_id,webhook_id,{source.secret_column},status"
        result = self._sb.execute_with_retry(
            lambda: self._platform_or_tool_query(source, cols).execute(),
            f"list active {source.table}",
        )
        return list(result.data or [])

    def _fetch_active_rows_for_org(
        self, source: WebhookSecretSource, org_id: str
    ) -> list:
        cols = f"organization_id,webhook_id,{source.secret_column},status"
        result = self._sb.execute_with_retry(
            lambda: self._platform_or_tool_query(source, cols)
            .eq("organization_id", org_id)
            .execute(),
            f"list active {source.table} for org {org_id}",
        )
        return list(result.data or [])

    def _platform_or_tool_query(self, source: WebhookSecretSource, cols: str):
        """Build the select query; platform table needs provider+tool filters."""
        q = (
            self._sb.table(source.table)
            .select(cols)
            .eq("status", source.status_value)
        )
        if source.prefer_platform_table:
            q = q.eq("provider", source.provider).eq("tool_slug", source.tool_slug)
        return q

    async def _sync_rows(
        self, source: WebhookSecretSource, rows: list
    ) -> SecretSyncResult:
        out = SecretSyncResult()
        by_org: Dict[str, Dict[str, str]] = defaultdict(dict)
        for row in rows:
            org_id = row.get("organization_id")
            webhook_id = row.get("webhook_id")
            raw_secret = row.get(source.secret_column)
            if not org_id or not webhook_id:
                out.rows_skipped += 1
                continue
            try:
                plaintext = _decrypt_secret(self._crypto, raw_secret)
            except CryptoError as exc:
                log.error(
                    "webhook_gateway: decrypt failed tool=%s org=%s webhook=%s: %s",
                    source.tool_slug,
                    org_id,
                    webhook_id,
                    exc,
                )
                out.errors += 1
                continue
            if not plaintext:
                out.rows_skipped += 1
                continue
            by_org[str(org_id)][str(webhook_id)] = plaintext

        for org_id, secrets in by_org.items():
            try:
                await self._store.put_secrets(
                    org_id,
                    source.tool_slug,
                    secrets,
                    provider=source.provider,
                    ttl_s=self._ttl_s,
                )
                out.orgs_synced += 1
                out.secrets_cached += len(secrets)
            except Exception as exc:
                log.error(
                    "webhook_gateway: put_secrets failed tool=%s org=%s: %s",
                    source.tool_slug,
                    org_id,
                    exc,
                )
                out.errors += 1
        return out

    async def refresh_all(self) -> SecretSyncResult:
        total = SecretSyncResult()
        for source in self._sources:
            try:
                rows = self._fetch_active_rows(source)
            except Exception as exc:
                log.error(
                    "webhook_gateway: secret sync failed to load %s: %s",
                    source.table,
                    exc,
                )
                total.errors += 1
                continue
            part = await self._sync_rows(source, rows)
            total.merge(part)

        log.info(
            "webhook_gateway: secret sync complete sources=%d orgs=%d secrets=%d "
            "skipped=%d errors=%d",
            len(self._sources),
            total.orgs_synced,
            total.secrets_cached,
            total.rows_skipped,
            total.errors,
        )
        return total

    async def refresh_org(self, org_id: str) -> SecretSyncResult:
        """Refresh a single org across all configured tool sources."""
        total = SecretSyncResult()
        for source in self._sources:
            try:
                rows = self._fetch_active_rows_for_org(source, org_id)
            except Exception as exc:
                log.error(
                    "webhook_gateway: secret sync failed for org=%s tool=%s: %s",
                    org_id,
                    source.tool_slug,
                    exc,
                )
                total.errors += 1
                continue

            secrets: Dict[str, str] = {}
            for row in rows:
                webhook_id = row.get("webhook_id")
                if not webhook_id:
                    total.rows_skipped += 1
                    continue
                try:
                    plaintext = _decrypt_secret(
                        self._crypto, row.get(source.secret_column)
                    )
                except CryptoError as exc:
                    log.error(
                        "webhook_gateway: decrypt failed tool=%s org=%s webhook=%s: %s",
                        source.tool_slug,
                        org_id,
                        webhook_id,
                        exc,
                    )
                    total.errors += 1
                    continue
                if not plaintext:
                    total.rows_skipped += 1
                    continue
                secrets[str(webhook_id)] = plaintext

            if secrets:
                await self._store.put_secrets(
                    org_id,
                    source.tool_slug,
                    secrets,
                    provider=source.provider,
                    ttl_s=self._ttl_s,
                )
                total.orgs_synced += 1
                total.secrets_cached += len(secrets)
            else:
                await self._store.invalidate(
                    org_id, source.tool_slug, provider=source.provider
                )
        return total
