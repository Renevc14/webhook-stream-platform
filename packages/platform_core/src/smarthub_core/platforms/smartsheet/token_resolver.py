"""Smartsheet impl of the shared ``TokenResolver`` interface.

Resolves the Smartsheet PAT and regional API base for ``(org_id, product_slug)``
through cache-aside on Redis, falling back to Postgres on miss. Tokens are
**always cached as ciphertext** under ``ss-token:{org}:{slug}``; decryption
happens in-process.

Source of truth: the ``smartsheet_tokens`` table (UNIQUE on
(organization_id, product_slug)) and ``organization_ss_api_base`` for the
regional base URL.

This class is also the engine behind ``SmartsheetClient._resolve_credentials``
— the client delegates here so there is one source of truth for resolution.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from smarthub_core.crypto import decrypt_token
from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenResolver,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)
from smarthub_core.settings import CoreSettings, get_core_settings

log = logging.getLogger(__name__)


_DEFAULT_TTL_S = 600

# ---------------------------------------------------------------------------
# encrypt-tool token broker (Smartsheet OAuth migration)
# ---------------------------------------------------------------------------
# US/EU plans authenticate with per-user, per-tool OAuth tokens that only the
# encrypt-tool credentials service can decrypt. When the broker env vars are
# set, resolution goes broker-first; the broker refreshes expiring tokens
# server-side and falls back to the org PAT itself, so this stays the single
# front door. Plaintext broker results are cached in-process only (never
# Redis) for a short window.
#
# SH-182: every broker response carries an explicit OAuth-state contract
# (source / oauth_state / fallback_used / fallback_reason) which is preserved
# in ResolvedToken.metadata, and an authoritative revoked/terminal answer with
# no transition PAT surfaces as TokenRevokedError instead of silently trying
# the legacy tables.
#
# A configured broker that cannot answer is a third outcome, separate from both
# "connected" and "not connected": it surfaces as
# TokenTemporarilyUnavailableError once the legacy tables come up empty too.
# Without that split, a broker blip reads as "no credentials", which tells a
# connected user to reconnect and lets callers retire retryable work.

_BROKER_CACHE_TTL_S = 60
# value: (monotonic_ts, access_token, base_url, metadata)
_broker_cache: Dict[Tuple[str, str, str], Tuple[float, str, str, Dict[str, Any]]] = {}

# Response fields safe to surface in ResolvedToken.metadata — identity and
# state only, never token material.
_BROKER_METADATA_FIELDS = (
    "source",
    "oauth_state",
    "fallback_used",
    "fallback_reason",
    "smartsheet_user_email",
    "region",
)


def _broker_env() -> Tuple[str, str]:
    return (
        (os.getenv("ENCRYPT_TOOL_INTERNAL_URL") or "").strip().rstrip("/"),
        (os.getenv("SMARTSHEET_BROKER_SECRET") or "").strip(),
    )


def broker_configured() -> bool:
    base, secret = _broker_env()
    return bool(base and secret)


class _BrokerUnavailable(Exception):
    """Internal: the broker was configured but could not answer.

    Not part of the public surface. ``resolve`` catches it, tries the legacy
    PAT tables, and only if those are empty too converts it into a public
    ``TokenTemporarilyUnavailableError`` — so a broker blip on an org that
    still has a PAT stays invisible.
    """


async def _broker_resolve(
    org_id: str,
    product_slug: str,
    user_id: Optional[str],
    *,
    force_refresh: bool = False,
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """(access_token, base_url, metadata) via the broker, or None for legacy.

    Returns None only when the broker is **unconfigured** — the deployment
    intends to use the PAT tables. When it is configured but cannot answer
    (transport error, 5xx, contract-violating 200) this raises
    ``_BrokerUnavailable`` so the caller can tell "we could not ask" apart from
    "there is nothing to find". Raises on authoritative broker answers where a
    second legacy lookup would be wrong:

    - 404 → TokenNotFoundError: no OAuth token and no PAT exist, so the
      legacy path would fail too.
    - 409 → TokenRevokedError: the grant is revoked or terminally failed and
      there is no transition PAT (SH-182). Falling back to a local PAT read
      would silently mask the revocation, so this propagates as a typed
      error carrying ``oauth_state`` and ``reason``.
    """
    base, secret = _broker_env()
    if not base or not secret:
        return None

    cache_key = (org_id, product_slug, user_id or "")
    if not force_refresh:
        hit = _broker_cache.get(cache_key)
        if hit and (time.monotonic() - hit[0]) < _BROKER_CACHE_TTL_S:
            return hit[1], hit[2], hit[3]

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(
                f"{base}/internal/token/resolve",
                json={
                    "organization_id": org_id,
                    "product_slug": product_slug,
                    "user_id": user_id or "",
                    "force_refresh": force_refresh,
                },
                headers={"X-Broker-Secret": secret},
            )
    except Exception as e:
        log.warning("smartsheet token broker unreachable (%s) — legacy path", e)
        raise _BrokerUnavailable(f"broker unreachable: {e}") from e

    if resp.status_code == 200:
        data = resp.json()
        token, base_url = data.get("access_token"), data.get("base_url")
        if token and base_url:
            metadata = {
                k: data[k] for k in _BROKER_METADATA_FIELDS if data.get(k) is not None
            }
            _broker_cache[cache_key] = (time.monotonic(), token, base_url, metadata)
            return token, base_url, metadata
        log.error(
            "smartsheet token broker returned 200 without access_token/base_url "
            "for org=%s slug=%s — treating as unavailable",
            org_id,
            product_slug,
        )
        raise _BrokerUnavailable("broker returned 200 without access_token/base_url")
    if resp.status_code == 404:
        raise TokenNotFoundError(
            f"No Smartsheet credentials for org={org_id} slug={product_slug} "
            "(connect the tool via Profile → Connect)"
        )
    if resp.status_code == 409:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        raise TokenRevokedError(
            f"Smartsheet OAuth connection revoked for org={org_id} "
            f"slug={product_slug} with no transition PAT — reconnect required",
            oauth_state=str(body.get("oauth_state") or "revoked"),
            reason=str(body.get("error_code") or "CONNECTION_REVOKED"),
        )
    log.warning("smartsheet token broker returned %s — legacy path", resp.status_code)
    raise _BrokerUnavailable(f"broker returned {resp.status_code}")


class SmartsheetTokenResolver(TokenResolver):
    """Cache-aside resolver for Smartsheet credentials.

    Cache key:    ``ss-token:{org_id}:{product_slug}``
    Cache shape:  ``{"ciphertext": str, "base_url": str}`` (JSON, ciphertext only)
    TTL:          600 seconds (invalidated explicitly on rotation)
    Fallback:     direct Postgres read + local decrypt when Redis is down

    Usage:
        resolver = SmartsheetTokenResolver(redis=rc, supabase=svc)
        token = await resolver.resolve("org-uuid", "smart-image-to-cell")
        # token.access_token is plaintext; token.base_url is regional URL
    """

    def __init__(
        self,
        *,
        redis: RedisClient,
        supabase: SupabaseService,
        settings: Optional[CoreSettings] = None,
        cache_ttl_s: int = _DEFAULT_TTL_S,
    ) -> None:
        self._redis = redis
        self._supabase = supabase
        self._settings = settings or get_core_settings()
        self._cache_ttl_s = cache_ttl_s

    @property
    def provider(self) -> str:
        return "smartsheet"

    @staticmethod
    def cache_key(org_id: str, product_slug: str) -> str:
        return f"ss-token:{org_id}:{product_slug}"

    async def resolve(
        self,
        org_id: str,
        product_slug: str,
        *,
        force_refresh: bool = False,
        user_id: Optional[str] = None,
    ) -> ResolvedToken:
        if not org_id or not product_slug:
            raise TokenNotFoundError("org_id and product_slug are required")

        # Broker-first: per-user OAuth token (US/EU) with server-side PAT
        # fallback. None → broker not configured, use the legacy tables below.
        # Authoritative revoked/terminal answers raise TokenRevokedError and
        # must never fall through to the legacy PAT read below.
        broker_error: Optional[str] = None
        try:
            brokered = await _broker_resolve(
                org_id, product_slug, user_id, force_refresh=force_refresh
            )
        except _BrokerUnavailable as e:
            # A PAT may still cover this org, so try the legacy tables — but
            # remember why we are here, so an empty result is reported as
            # "ask again later" instead of "connect the tool".
            brokered, broker_error = None, str(e)

        if brokered is not None:
            access_token, broker_base_url, broker_meta = brokered
            return ResolvedToken(
                access_token=access_token,
                base_url=broker_base_url,
                metadata=broker_meta,
            )

        key = self.cache_key(org_id, product_slug)

        if not force_refresh:
            cached = await self._redis.cache_get(key)
            if cached is not None:
                try:
                    parsed = json.loads(cached)
                    return ResolvedToken(
                        access_token=decrypt_token(parsed["ciphertext"]),
                        base_url=parsed["base_url"],
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning(
                        "smartsheet_token_resolver: corrupt cache for org=%s slug=%s — re-fetching: %s",
                        org_id,
                        product_slug,
                        e,
                    )
                    await self._redis.cache_del(key)

        try:
            ciphertext, base_url = await self._fetch_from_postgres(org_id, product_slug)
        except TokenNotFoundError as e:
            if broker_error is not None:
                raise TokenTemporarilyUnavailableError(
                    f"Smartsheet credentials service unavailable for org={org_id} "
                    f"slug={product_slug} ({broker_error}) and no fallback token "
                    "exists — retry later"
                ) from e
            raise

        cache_payload = json.dumps({"ciphertext": ciphertext, "base_url": base_url})
        await self._redis.cache_set(key, cache_payload, ttl_s=self._cache_ttl_s)

        return ResolvedToken(
            access_token=decrypt_token(ciphertext),
            base_url=base_url,
        )

    async def invalidate(self, org_id: str, product_slug: str) -> None:
        for key in [k for k in _broker_cache if k[0] == org_id and k[1] == product_slug]:
            _broker_cache.pop(key, None)
        await self._redis.cache_del(self.cache_key(org_id, product_slug))

    async def _fetch_from_postgres(
        self,
        org_id: str,
        product_slug: str,
    ) -> Tuple[str, str]:
        def _read_token():
            return (
                self._supabase.table("smartsheet_tokens")
                .select("encrypted_token")
                .eq("organization_id", org_id)
                .eq("product_slug", product_slug)
                .limit(1)
                .execute()
            )

        token_result = await self._supabase.execute_with_retry_async(
            _read_token,
            description=f"smartsheet_tokens get org={org_id} slug={product_slug}",
        )
        if not token_result.data:
            raise TokenNotFoundError(
                f"No Smartsheet token for org={org_id} slug={product_slug}"
            )
        ciphertext = token_result.data[0]["encrypted_token"]

        def _read_base_url():
            return (
                self._supabase.table("organization_ss_api_base")
                .select("base_url")
                .eq("organization_id", org_id)
                .limit(1)
                .execute()
            )

        base_result = await self._supabase.execute_with_retry_async(
            _read_base_url,
            description=f"organization_ss_api_base get org={org_id}",
        )
        if not base_result.data or not base_result.data[0].get("base_url"):
            raise TokenNotFoundError(
                f"No base_url configured for org={org_id} — set ss_region first"
            )
        base_url = base_result.data[0]["base_url"]

        return ciphertext, base_url
