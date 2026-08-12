"""Webhook shared-secret lookup (Redis cache-aside).

SH-173: the lifecycle manager populates the cache as a JSON map
``{webhook_id: plaintext_secret}`` (TTL 300s) so HMAC verification never hits
Supabase on the hot path. Ciphertext stays in Postgres; Redis holds plaintext
only long enough for verification (same contract as the SH-153 E2E seed and
TARGET_ARCHITECTURE secret cache, plaintext-on-read variant).

SH-215: the key is namespaced by provider and its format is owned by
``smarthub_core.webhooks.secret_cache``. There is no compatibility read for the
pre-SH-215 key: entries expire in one TTL and the lifecycle sync rebuilds them
from Postgres on the next cycle, which runs at the same interval.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Dict, Mapping, Optional

from smarthub_core.webhooks.secret_cache import (
    DEFAULT_PROVIDER,
    DEFAULT_SECRETS_TTL_S,
    SECRET_CACHE_KEY,
    secret_cache_key,
)

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_SECRETS_TTL_S",
    "SECRET_CACHE_KEY",
    "InMemoryWebhookSecretStore",
    "RedisWebhookSecretStore",
    "WebhookSecretStore",
    "secret_cache_key",
]


def _coerce_map(data: object) -> Dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


class WebhookSecretStore(ABC):
    """Returns decrypted shared secrets keyed by provider webhook id.

    Subclasses implement the three storage primitives so every backend behaves
    identically.
    """

    @abstractmethod
    async def _read_map(self, key: str) -> Dict[str, str]:
        """Return the cached map, or an empty dict on miss/unreadable."""

    @abstractmethod
    async def _write_map(
        self, key: str, payload: Mapping[str, str], ttl_s: int
    ) -> None:
        """Overwrite the map at ``key`` with the given TTL."""

    @abstractmethod
    async def _delete_map(self, key: str) -> None:
        """Remove ``key``; a miss is not an error."""

    async def get_secrets(
        self,
        org_id: str,
        tool_slug: str,
        *,
        provider: str = DEFAULT_PROVIDER,
    ) -> Dict[str, str]:
        """Map webhook_id to shared_secret for HMAC verification."""
        return await self._read_map(secret_cache_key(org_id, tool_slug, provider))

    async def put_secrets(
        self,
        org_id: str,
        tool_slug: str,
        secrets: Mapping[str, str],
        *,
        provider: str = DEFAULT_PROVIDER,
        ttl_s: int = DEFAULT_SECRETS_TTL_S,
    ) -> None:
        """Write the webhook_id to plaintext secret map (overwrites)."""
        payload = {str(k): str(v) for k, v in secrets.items() if v}
        if not payload:
            await self.invalidate(org_id, tool_slug, provider=provider)
            return
        await self._write_map(
            secret_cache_key(org_id, tool_slug, provider), payload, ttl_s
        )

    async def invalidate(
        self,
        org_id: str,
        tool_slug: str,
        *,
        provider: str = DEFAULT_PROVIDER,
    ) -> None:
        """Delete the cached map so the next cycle / miss rebuilds it."""
        await self._delete_map(secret_cache_key(org_id, tool_slug, provider))


class InMemoryWebhookSecretStore(WebhookSecretStore):
    """Test/dev store backed by an in-memory dict keyed by cache key."""

    def __init__(self, secrets: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self._secrets: Dict[str, Dict[str, str]] = {
            k: dict(v) for k, v in (secrets or {}).items()
        }

    async def _read_map(self, key: str) -> Dict[str, str]:
        return dict(self._secrets.get(key, {}))

    async def _write_map(
        self, key: str, payload: Mapping[str, str], ttl_s: int
    ) -> None:
        # TTL is ignored in-memory — tests control lifetime explicitly.
        _ = ttl_s
        self._secrets[key] = dict(payload)

    async def _delete_map(self, key: str) -> None:
        self._secrets.pop(key, None)


class RedisWebhookSecretStore(WebhookSecretStore):
    """Reads/writes pre-populated secrets in Redis (lifecycle manager fills)."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def _read_map(self, key: str) -> Dict[str, str]:
        try:
            raw = await self._redis.cache_get(key)
        except Exception as exc:
            log.warning(
                "webhook_gateway: Redis secret lookup failed key=%s: %s", key, exc
            )
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.error("webhook_gateway: invalid JSON in secret cache key=%s", key)
            return {}
        return _coerce_map(data)

    async def _write_map(
        self, key: str, payload: Mapping[str, str], ttl_s: int
    ) -> None:
        ok = await self._redis.cache_set(key, json.dumps(dict(payload)), ttl_s)
        if not ok:
            log.error(
                "webhook_gateway: failed to cache secrets key=%s count=%d",
                key,
                len(payload),
            )
            return
        log.info(
            "webhook_gateway: cached secrets key=%s count=%d ttl=%ds",
            key,
            len(payload),
            ttl_s,
        )

    async def _delete_map(self, key: str) -> None:
        deleted = await self._redis.cache_del(key)
        if deleted:
            log.info("webhook_gateway: invalidated secrets key=%s", key)
