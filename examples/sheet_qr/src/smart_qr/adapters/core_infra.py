"""smarthub_core wiring — the only place SmartQR constructs core objects.

SmartQR owns its own storage (asyncpg + `smartqr_*` tables) and its own Redis
usage, but two things are platform concerns rather than tool concerns:

* **Whose Smartsheet credentials are these?** Resolution goes through the
  encrypt-tool broker, honours revocation, and falls back to a regional PAT.
  Reimplementing that per tool is how one tool ends up writing with an identity
  the user already disconnected.
* **How do we call Smartsheet?** Retry classification, the 401 refresh dance and
  the sliding-window rate limit are shared, and the rate limit only works if
  every caller for an org goes through the same Redis key.

Both live in ``smarthub_core.platforms.smartsheet``. SmartQR used to carry its
own copy of each; this module replaces those copies.

## Why the concrete imports are lazy

``smarthub_core.platforms.smartsheet`` pulls in the Supabase SDK and Redis at
import time. SmartQR runs in mock mode locally and its unit tests inject fakes,
so neither should need those packages importable just to load a module. Only
``platforms.shared.token_resolver`` (pure stdlib) is imported eagerly, because
its exception types are part of SmartQR's error mapping.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional
from uuid import UUID

from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)

from smart_qr.config.settings import Settings

log = logging.getLogger(__name__)

# A Smartsheet SDK client is rebuilt at most this often per (org, user). Long
# enough that a backfill reuses one client for its whole run, short enough that
# a revoked grant stops working within minutes even if Smartsheet never answers
# 401 (which is what would otherwise force a refresh).
CLIENT_TTL_S = 300.0
# Bounded so a long-lived task serving many users cannot leak clients.
CLIENT_CACHE_MAX = 256


class _NoLegacyTokens:
    """Stands in for ``SupabaseService`` in core's PAT fallback path.

    SmartQR launched OAuth-only: there is no ``smart-qr`` row in
    ``smartsheet_tokens`` and there is not meant to be one. Regions without an
    OAuth app (GOV/AUS) are covered by the broker, which falls back to the org
    PAT server-side, so the *client-side* legacy read can only ever come up
    empty for this tool.

    Answering "no rows" instead of wiring a real Supabase client keeps core's
    fallback total — it raises its own ``TokenNotFoundError``, which core then
    reports as temporary when the broker was the thing that failed — without
    opening a Supabase connection pool SmartQR does not otherwise need.
    """

    class _Empty:
        data: list[dict[str, Any]] = []

    class _Query:
        def select(self, *_a: Any, **_k: Any) -> "_NoLegacyTokens._Query":
            return self

        def eq(self, *_a: Any, **_k: Any) -> "_NoLegacyTokens._Query":
            return self

        def limit(self, *_a: Any, **_k: Any) -> "_NoLegacyTokens._Query":
            return self

        def execute(self) -> "_NoLegacyTokens._Empty":
            return _NoLegacyTokens._Empty()

    def table(self, _name: str) -> "_NoLegacyTokens._Query":
        return self._Query()

    async def execute_with_retry_async(
        self, operation: Any, description: str = ""
    ) -> Any:
        return operation()


class _CredentialResolverAsCore:
    """Presents SmartQR's ``CredentialResolver`` as a core ``TokenResolver``.

    ``SmartsheetClient`` only talks to the core interface. Routing every mode
    (broker, static local PAT, unconfigured) through that interface means the
    retry / rate-limit envelope never has a second way of learning credentials —
    which is exactly how a local PAT used to be invisible to a client that
    resolved through the broker on its own.
    """

    def __init__(self, credential_resolver: Any) -> None:
        self._inner = credential_resolver

    async def resolve(
        self,
        org_id: str,
        product_slug: str,
        *,
        force_refresh: bool = False,
        user_id: Optional[str] = None,
    ) -> ResolvedToken:
        # Local import: credentials imports this module for the Redis/broker
        # wiring, so a top-level import would cycle.
        from smart_qr.adapters.smartsheet.credentials import (
            CredentialsMissing,
            CredentialsMisconfigured,
            CredentialsRevoked,
            CredentialsTemporarilyUnavailable,
        )

        try:
            credentials = await self._inner.resolve(
                UUID(org_id), user_id, force_refresh=force_refresh
            )
        except CredentialsRevoked as exc:
            raise TokenRevokedError(
                str(exc),
                oauth_state=getattr(exc, "oauth_state", "revoked"),
                reason=getattr(exc, "reason", "CONNECTION_REVOKED"),
            ) from exc
        except CredentialsTemporarilyUnavailable as exc:
            raise TokenTemporarilyUnavailableError(str(exc)) from exc
        except CredentialsMisconfigured as exc:
            # Our own deployment is wrong. Retrying will not help, but neither
            # will telling the user to reconnect — surface it as temporary so
            # operators see it in the logs without a false "connect Smartsheet".
            raise TokenTemporarilyUnavailableError(str(exc)) from exc
        except CredentialsMissing as exc:
            raise TokenNotFoundError(str(exc)) from exc

        return ResolvedToken(
            access_token=credentials.access_token,
            base_url=credentials.base_url,
            metadata={
                "source": credentials.source,
                "oauth_state": credentials.oauth_state,
                **dict(credentials.metadata or {}),
            },
        )

    async def invalidate(self, org_id: str, product_slug: str) -> None:
        await self._inner.invalidate(UUID(org_id))


def _core_settings(settings: Settings) -> Any:
    """CoreSettings built from SmartQR's own settings.

    Explicitly constructed rather than read from the environment: SmartQR loads
    its config through pydantic (which reads ``.env`` files that never reach
    ``os.environ``), so relying on core's env-based singleton would silently use
    different values locally than the ones SmartQR is running with.
    """
    from smarthub_core.settings import CoreSettings

    return CoreSettings(
        app_crypto_key=settings.app_crypto_key,
        redis_url=settings.redis_url,
        supabase_url=settings.supabase_url or None,
        supabase_service_role_key=settings.supabase_service_role_key or None,
    )


def _export_broker_env(settings: Settings) -> None:
    """Make the broker config visible to core, which reads it from the environment.

    In staging these are already real environment variables from the task
    definition; locally they come from ``.env`` via pydantic and would otherwise
    be invisible to ``os.getenv``, silently sending core down the PAT path.
    """
    for name, value in (
        ("ENCRYPT_TOOL_INTERNAL_URL", settings.encrypt_tool_internal_url),
        ("SMARTSHEET_BROKER_SECRET", settings.smartsheet_broker_secret),
    ):
        cleaned = (value or "").strip()
        if cleaned and not (os.environ.get(name) or "").strip():
            os.environ[name] = cleaned


_redis: Optional[Any] = None
_core_token_resolver: Optional[Any] = None
# (org, user) -> (created_monotonic, SmartsheetClient)
_clients: dict[tuple[str, str], tuple[float, Any]] = {}


async def get_core_redis(settings: Settings) -> Any:
    """The shared core RedisClient, started on first use.

    Separate from the ad-hoc ``redis.asyncio`` clients elsewhere in SmartQR only
    because core owns its pool configuration; both point at the same server, so
    the rate-limit key really is shared with the other tools.
    """
    global _redis
    if _redis is None:
        from smarthub_core.db.redis import RedisClient

        core = _core_settings(settings)
        client = RedisClient(
            core.redis_url,
            max_connections=core.redis_max_connections,
            socket_timeout_s=core.redis_socket_timeout_s,
        )
        await client.start()
        _redis = client
        log.info("smarthub_core Redis client started for Smartsheet rate limiting")
    return _redis


async def get_token_resolver(settings: Settings) -> Any:
    """The shared ``SmartsheetTokenResolver`` — broker-first, revocation-aware.

    Used by ``CoreCredentialResolver``. Live Smartsheet calls go through
    ``get_smartsheet_client``, which wraps SmartQR's own credential resolver
    instead, so a local static PAT still reaches the shared client.
    """
    global _core_token_resolver
    if _core_token_resolver is None:
        _export_broker_env(settings)
        from smarthub_core.platforms.smartsheet.token_resolver import (
            SmartsheetTokenResolver,
            broker_configured,
        )

        if not broker_configured():
            # Not fatal here (mock mode never resolves anything), but outside a
            # laptop it means every Smartsheet call is about to fail closed.
            report = log.info if settings.is_local else log.error
            report(
                "No Smartsheet credential broker configured "
                "(ENCRYPT_TOOL_INTERNAL_URL + SMARTSHEET_BROKER_SECRET) — env=%s",
                settings.app_env,
            )
        _core_token_resolver = SmartsheetTokenResolver(
            redis=await get_core_redis(settings),
            supabase=_NoLegacyTokens(),
            settings=_core_settings(settings),
        )
    return _core_token_resolver


async def get_smartsheet_client(
    settings: Settings,
    organization_id: UUID,
    user_id: Optional[str] = None,
    *,
    credential_resolver: Optional[Any] = None,
) -> Any:
    """A core ``SmartsheetClient`` scoped to this organization and user.

    Cached briefly per identity so a backfill does not rebuild an SDK client per
    row, and never shared across identities: the whole point of resolving per
    (org, user) is that one org's token must not reach another org's sheets.

    Credentials always come from SmartQR's ``CredentialResolver`` (broker,
    static local PAT, or the fail-closed unconfigured stub). That is the single
    source of identity for every Smartsheet call this process makes.
    """
    from smarthub_core.platforms.smartsheet.client import SmartsheetClient

    if credential_resolver is None:
        from smart_qr.adapters.smartsheet.credentials import get_credential_resolver

        credential_resolver = get_credential_resolver(settings)

    key = (str(organization_id), str(user_id or ""))
    now = time.monotonic()
    hit = _clients.get(key)
    if hit is not None and (now - hit[0]) < CLIENT_TTL_S:
        return hit[1]

    client = SmartsheetClient(
        str(organization_id),
        settings.product_slug,
        redis=await get_core_redis(settings),
        supabase=_NoLegacyTokens(),
        settings=_core_settings(settings),
        token_resolver=_CredentialResolverAsCore(credential_resolver),  # type: ignore[arg-type]
        owner_user_id=str(user_id) if user_id else None,
    )

    if len(_clients) >= CLIENT_CACHE_MAX:
        oldest = min(_clients, key=lambda k: _clients[k][0])
        _clients.pop(oldest, None)
    _clients[key] = (now, client)
    return client


def forget_smartsheet_client(
    organization_id: UUID, user_id: Optional[str] = None
) -> None:
    """Drop the cached client for an identity (used after a credential change)."""
    _clients.pop((str(organization_id), str(user_id or "")), None)


async def close_core_infra() -> None:
    """Release core resources on shutdown. Safe to call more than once."""
    global _redis, _core_token_resolver
    _clients.clear()
    _core_token_resolver = None
    if _redis is not None:
        try:
            await _redis.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to close core Redis client: %s", exc)
        _redis = None
