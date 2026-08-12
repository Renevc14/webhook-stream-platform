#!/usr/bin/env python3
"""
smartsheet_broker.py
────────────────────────────────────────────────────────────────────────────
Client for the encrypt-tool token broker (POST /internal/token/resolve).

US/EU plans authenticate with per-user, per-tool Smartsheet OAuth tokens
that live (encrypted) in core_api_keys.smartsheet_oauth_tokens and are only
ever decrypted inside the credentials service (encrypt-tool). Tool backends
call this broker to obtain a short-lived plaintext access token plus the
regional base URL. The broker transparently refreshes expiring tokens and
falls back to the legacy org PAT when no OAuth token exists, so callers can
use it as the single front door for credentials.

If the broker is not configured (missing env) or unreachable, callers should
fall back to their legacy smartsheet_tokens + APP_CRYPTO_KEY decrypt path —
that keeps GOV/AUS plans and un-migrated environments fully working.

ENV
───
    ENCRYPT_TOOL_INTERNAL_URL   e.g. http://localhost:3051 (no trailing slash)
    SMARTSHEET_BROKER_SECRET    shared secret for X-Broker-Secret header

This module is duplicated across tool backends (same pattern as
crypto_helper.py) so each service stays independently deployable.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional, Tuple

import requests

log = logging.getLogger("smartsheet-broker")

_BROKER_TIMEOUT_SECONDS = 12

# Tiny in-process cache so bursty endpoints (pickers walking folders) don't
# hammer the broker. Access tokens live for days; 60s of caching is safe and
# revocation still lands within a minute.
_CACHE_TTL_SECONDS = 60
_cache: dict[tuple, tuple[float, dict]] = {}


def broker_configured() -> bool:
    return bool(
        (os.getenv("ENCRYPT_TOOL_INTERNAL_URL") or "").strip()
        and (os.getenv("SMARTSHEET_BROKER_SECRET") or "").strip()
    )


def resolve_smartsheet_credentials(
    organization_id: str,
    product_slug: str,
    user_id: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> Optional[Tuple[str, str]]:
    """
    Resolve (access_token, base_url) for a tool run via the credentials
    service. Prefers the calling user's OAuth token for this tool; the broker
    falls back to the org PAT server-side. Returns None when the broker is
    not configured, unreachable, or has no credentials — the caller should
    then try its own legacy path.
    """
    base = (os.getenv("ENCRYPT_TOOL_INTERNAL_URL") or "").strip().rstrip("/")
    secret = (os.getenv("SMARTSHEET_BROKER_SECRET") or "").strip()
    if not base or not secret:
        return None

    cache_key = (organization_id, product_slug, user_id or "")
    if not force_refresh:
        hit = _cache.get(cache_key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
            data = hit[1]
            return data["access_token"], data["base_url"]

    try:
        resp = requests.post(
            f"{base}/internal/token/resolve",
            json={
                "organization_id": organization_id,
                "product_slug": product_slug,
                "user_id": user_id or "",
                "force_refresh": force_refresh,
            },
            headers={"X-Broker-Secret": secret},
            timeout=_BROKER_TIMEOUT_SECONDS,
        )
    except Exception as e:
        log.warning("broker unreachable (%s) — falling back to legacy path", e)
        return None

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            return None
        token = data.get("access_token")
        base_url = data.get("base_url")
        if token and base_url:
            _cache[cache_key] = (time.monotonic(), data)
            log.info(
                "resolved %s credentials for org=%s slug=%s user=%s",
                data.get("source", "?"), organization_id[:8], product_slug,
                (user_id or "")[:8],
            )
            return token, base_url
        return None

    if resp.status_code == 404:
        # Broker is authoritative: no OAuth token AND no PAT. Don't retry the
        # legacy path (it would hit the same empty table); surface cleanly.
        log.info(
            "broker: no credentials for org=%s slug=%s user=%s",
            organization_id[:8], product_slug, (user_id or "")[:8],
        )
        raise NoSmartsheetCredentials(product_slug)

    log.warning("broker returned %s — falling back to legacy path", resp.status_code)
    return None


def invalidate_cached_credentials(
    organization_id: str, product_slug: str, user_id: Optional[str] = None
) -> None:
    """Drop the cached entry (e.g. after a 401 from Smartsheet)."""
    _cache.pop((organization_id, product_slug, user_id or ""), None)


class NoSmartsheetCredentials(RuntimeError):
    """Raised when the broker definitively reports no credentials (404)."""

    def __init__(self, product_slug: str):
        super().__init__(
            "Smartsheet is not connected for this tool. "
            "Open Profile → Connect and sign in with Smartsheet "
            f"(tool: {product_slug})."
        )
        self.product_slug = product_slug
