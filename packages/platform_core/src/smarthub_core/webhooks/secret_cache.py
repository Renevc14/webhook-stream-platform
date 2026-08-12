"""Shared naming for the webhook shared-secret cache (SH-215).

The Webhook Gateway verifies HMAC signatures against a short-TTL Redis map of
``{webhook_id: plaintext_secret}``. Several processes write it — the gateway's
pull-sync, the gateway's registrar, and tools pushing over the internal m2m
route — so the key format is a contract, not an implementation detail. It lives
here so no side can drift from the others.

Key::

    webhook-secrets:{org_id}:{provider}:{tool_slug}

The ``provider`` segment matters even though every provider is Smartsheet
today: the gateway verifies by trying each cached secret until one validates,
so a tool listening to two providers with one shared map could validate a
request against a secret issued by the other provider.

Redis holds plaintext only long enough to verify; ciphertext stays in Postgres,
which is the source of truth. That is also why there is no compatibility path
for the pre-SH-215 key: entries expire within one TTL and the lifecycle sync
rebuilds them from Postgres on its next cycle.
"""

from __future__ import annotations

DEFAULT_PROVIDER = "smartsheet"
DEFAULT_SECRETS_TTL_S = 300

SECRET_CACHE_KEY = "webhook-secrets:{org_id}:{provider}:{tool_slug}"


def secret_cache_key(
    org_id: str,
    tool_slug: str,
    provider: str = DEFAULT_PROVIDER,
) -> str:
    """Provider-scoped cache key for one org's webhook secrets."""
    return SECRET_CACHE_KEY.format(
        org_id=org_id,
        provider=(provider or DEFAULT_PROVIDER).strip().lower(),
        tool_slug=tool_slug,
    )
