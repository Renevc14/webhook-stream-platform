"""SH-215 — the secret cache key contract shared by the gateway and tools."""

from __future__ import annotations

import smarthub_core.webhooks as webhooks_pkg
from smarthub_core.webhooks.secret_cache import (
    DEFAULT_PROVIDER,
    DEFAULT_SECRETS_TTL_S,
    secret_cache_key,
)

ORG = "org-1"
TOOL = "smart-qr"


def test_key_is_org_provider_tool():
    assert secret_cache_key(ORG, TOOL, "smartsheet") == (
        "webhook-secrets:org-1:smartsheet:smart-qr"
    )


def test_provider_defaults_to_smartsheet():
    assert secret_cache_key(ORG, TOOL) == secret_cache_key(ORG, TOOL, DEFAULT_PROVIDER)


def test_provider_is_normalized():
    """The gateway builds this from a URL segment, so casing and stray spaces
    must not create a second key for the same provider."""
    assert secret_cache_key(ORG, TOOL, " SmartSheet ") == secret_cache_key(ORG, TOOL)


def test_blank_provider_falls_back_to_default():
    assert secret_cache_key(ORG, TOOL, "") == secret_cache_key(ORG, TOOL)


def test_providers_produce_distinct_keys():
    assert secret_cache_key(ORG, TOOL, "slack") != secret_cache_key(ORG, TOOL)


def test_no_legacy_key_helper_is_exported():
    """The pre-SH-215 key has no reader left: entries expire in one TTL and the
    lifecycle sync rebuilds them from Postgres. Re-adding a helper here is how a
    compatibility path creeps back in."""
    assert not hasattr(webhooks_pkg, "legacy_secret_cache_key")
    assert not hasattr(webhooks_pkg, "LEGACY_SECRET_CACHE_KEY")


def test_ttl_is_short_lived():
    """Redis holds plaintext; Postgres is the source of truth. A long TTL would
    let a revoked secret stay verifiable."""
    assert 0 < DEFAULT_SECRETS_TTL_S <= 900
