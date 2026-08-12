"""Smartsheet webhook callback URL helpers (shared gateway contract).

Gateway route shape (SH-175 / Phase 3 / SH-231):

  {base}/webhook/{provider}/{tool_slug}/{organization_id}

Default provider remains ``smartsheet``. Tools that still register a single
legacy ``WEBHOOK_CALLBACK_URL`` (direct receiver) keep working when
``gateway_base_url`` is unset. During cutover, ownership patterns include both
the gateway prefix and the legacy URL so zombie scans do not abandon in-flight
webhooks.
"""

from __future__ import annotations

from typing import List, Optional

DEFAULT_PROVIDER = "smartsheet"


def gateway_callback_url(
    base: str,
    product_slug: str,
    organization_id: str,
    *,
    provider: str = DEFAULT_PROVIDER,
) -> str:
    """Build the shared-gateway callback URL for one org + tool slug."""
    prov = (provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    return (
        f"{base.rstrip('/')}/webhook/{prov}/"
        f"{product_slug.strip('/')}/{organization_id}"
    )


def resolve_webhook_callback_url(
    *,
    organization_id: str,
    gateway_base_url: Optional[str],
    legacy_callback_url: Optional[str],
    product_slug: str,
    provider: str = DEFAULT_PROVIDER,
) -> Optional[str]:
    """Return the callback URL to register / enforce for an org.

    When ``gateway_base_url`` is set it wins (per-org gateway URL). Otherwise
    falls back to the legacy shared direct-receiver URL.
    """
    base = (gateway_base_url or "").strip()
    if base:
        if not organization_id:
            return None
        return gateway_callback_url(
            base, product_slug, organization_id, provider=provider
        )
    url = (legacy_callback_url or "").strip()
    return url or None


def webhook_callback_ownership_patterns(
    *,
    gateway_base_url: Optional[str],
    legacy_callback_url: Optional[str],
    product_slug: str,
    provider: str = DEFAULT_PROVIDER,
) -> List[str]:
    """Substrings that mark a webhook as owned by this deployment."""
    patterns: List[str] = []
    base = (gateway_base_url or "").strip()
    if base:
        prov = (provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
        patterns.append(
            f"{base.rstrip('/')}/webhook/{prov}/{product_slug.strip('/')}/"
        )
    legacy = (legacy_callback_url or "").strip()
    if legacy:
        patterns.append(legacy)
    return patterns


def is_our_webhook_callback(callback_url: str, patterns: List[str]) -> bool:
    """True when ``callback_url`` matches any ownership substring."""
    url = callback_url or ""
    return any(p and p in url for p in patterns)
