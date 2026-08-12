"""Smartsheet credentials per (organization, user).

SmartQR does not own Smartsheet credentials and does not talk to the credential
broker itself. ``smarthub_core.platforms.smartsheet.SmartsheetTokenResolver``
does: it asks the encrypt-tool broker for the signed-in user's per-tool OAuth
access token, lets the broker refresh it server-side, honours an authoritative
revocation, and returns the org's **regional** API base — which is why no call
site here may hardcode `api.smartsheet.com`: sending an EU token to the US host
leaks a credential to the wrong tenant and fails.

This module is the translation layer, and nothing else. Core answers in three
states and SmartQR has to react differently to each:

    connected            → ``SmartsheetCredentials``
    nothing connected    → ``CredentialsMissing``      → tell the user to connect
    grant revoked        → ``CredentialsRevoked``      → tell the user to reconnect
    broker cannot answer → ``CredentialsTemporarilyUnavailable`` → retry later

Collapsing the last two into "not connected" is the bug this taxonomy exists to
prevent: it asks a correctly-connected user to reconnect, and it lets callers
retire work that should have been retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from uuid import UUID

# Pure-stdlib module: importing the error contract must not drag in the Supabase
# or Redis SDKs, which the concrete resolver needs but mock mode does not.
from smarthub_core.platforms.shared.token_resolver import (
    TokenNotFoundError,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)

log = logging.getLogger(__name__)

# Only used when no org has a regional base (local mock/dev).
DEFAULT_API_BASE = "https://api.smartsheet.com/2.0"


@dataclass(frozen=True)
class SmartsheetCredentials:
    """A short-lived access token plus the regional API base it belongs to."""

    access_token: str
    base_url: str = DEFAULT_API_BASE
    source: str = "unknown"  # oauth | pat | static
    oauth_state: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


class CredentialsUnavailable(RuntimeError):
    """Base: SmartQR cannot act on Smartsheet for this org/user right now."""


class CredentialsMissing(CredentialsUnavailable):
    """Nothing is connected — the user must connect Smartsheet for this tool."""


class CredentialsRevoked(CredentialsUnavailable):
    """The grant was revoked or terminally failed; re-consent is required."""

    def __init__(self, message: str, *, oauth_state: str = "revoked", reason: str = "CONNECTION_REVOKED"):
        super().__init__(message)
        self.oauth_state = oauth_state
        self.reason = reason


class CredentialsTemporarilyUnavailable(CredentialsUnavailable):
    """The broker could not answer. Retryable — do not pause anything."""


class CredentialsMisconfigured(CredentialsUnavailable):
    """Our own deployment is wrong (missing/incorrect broker secret or URL)."""


class CredentialResolver(Protocol):
    async def resolve(
        self,
        organization_id: UUID,
        user_id: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> SmartsheetCredentials: ...

    async def invalidate(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> None: ...


class StaticCredentialResolver:
    """One service-wide token, for local development and mock mode.

    Kept deliberately dumb: it ignores the org and user, so it must never be
    selected outside `APP_ENV=local` — a shared token would let one org write
    with another org's credentials.
    """

    def __init__(self, access_token: str, base_url: str = DEFAULT_API_BASE) -> None:
        self._credentials = SmartsheetCredentials(
            access_token=access_token, base_url=base_url, source="static", oauth_state="n/a"
        )

    async def resolve(
        self,
        organization_id: UUID,
        user_id: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> SmartsheetCredentials:
        if not self._credentials.access_token:
            raise CredentialsMisconfigured(
                "SMARTSHEET_ACCESS_TOKEN is empty and no OAuth broker is configured"
            )
        return self._credentials

    async def invalidate(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> None:
        return None


class UnconfiguredCredentialResolver:
    """Fails closed where a shared token would be unacceptable.

    Reached when the broker is not configured outside `APP_ENV=local`. Refusing
    is the point: the alternative is one token acting for every organization.
    """

    def __init__(self, app_env: str) -> None:
        self._app_env = app_env

    async def resolve(
        self,
        organization_id: UUID,
        user_id: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> SmartsheetCredentials:
        raise CredentialsMisconfigured(
            "Smartsheet OAuth broker is not configured for this environment "
            f"(env={self._app_env}) — set ENCRYPT_TOOL_INTERNAL_URL and "
            "SMARTSHEET_BROKER_SECRET"
        )

    async def invalidate(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> None:
        return None


class CoreCredentialResolver:
    """Delegates to core's ``SmartsheetTokenResolver`` and maps its errors.

    The core resolver is built on first use rather than in the constructor: it
    needs a started Redis client, and construction happens while the FastAPI app
    is still assembling its dependency graph.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._core: Optional[Any] = None

    async def _resolver(self) -> Any:
        if self._core is None:
            from smart_qr.adapters.core_infra import get_token_resolver

            self._core = await get_token_resolver(self._settings)
        return self._core

    async def resolve(
        self,
        organization_id: UUID,
        user_id: Optional[str] = None,
        *,
        force_refresh: bool = False,
    ) -> SmartsheetCredentials:
        resolver = await self._resolver()
        try:
            resolved = await resolver.resolve(
                str(organization_id),
                self._settings.product_slug,
                force_refresh=force_refresh,
                user_id=str(user_id) if user_id else None,
            )
        except TokenRevokedError as exc:
            raise CredentialsRevoked(
                f"Smartsheet connection revoked for org={organization_id} — "
                "reconnect required",
                oauth_state=getattr(exc, "oauth_state", "revoked"),
                reason=getattr(exc, "reason", "CONNECTION_REVOKED"),
            ) from exc
        except TokenTemporarilyUnavailableError as exc:
            raise CredentialsTemporarilyUnavailable(
                f"Smartsheet credentials service unavailable ({exc})"
            ) from exc
        except TokenNotFoundError as exc:
            raise CredentialsMissing(
                f"Smartsheet is not connected for this organization (org={organization_id}). "
                "Open Profile → Connect and sign in with Smartsheet."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Infrastructure we depend on but do not control (Redis, the broker's
            # network path). Retryable by default: refusing forever because a
            # cache was unreachable would be worse than asking again.
            log.error(
                "Unexpected failure resolving Smartsheet credentials org=%s user=%s: %s",
                organization_id,
                user_id or "-",
                exc,
            )
            raise CredentialsTemporarilyUnavailable(
                f"Smartsheet credentials could not be resolved ({type(exc).__name__}: {exc})"
            ) from exc

        metadata = dict(resolved.metadata or {})
        credentials = SmartsheetCredentials(
            access_token=resolved.access_token,
            base_url=resolved.base_url or DEFAULT_API_BASE,
            source=str(metadata.get("source") or "unknown"),
            oauth_state=str(metadata.get("oauth_state") or "unknown"),
            metadata=metadata,
        )
        log.info(
            "Resolved Smartsheet credentials org=%s user=%s source=%s oauth_state=%s region=%s",
            organization_id,
            user_id or "-",
            credentials.source,
            credentials.oauth_state,
            metadata.get("region", "-"),
        )
        return credentials

    async def invalidate(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> None:
        """Drop cached credentials, e.g. after Smartsheet answered 401."""
        from smart_qr.adapters.core_infra import forget_smartsheet_client

        forget_smartsheet_client(organization_id, user_id)
        resolver = await self._resolver()
        await resolver.invalidate(str(organization_id), self._settings.product_slug)
        log.info(
            "Dropped cached Smartsheet credentials org=%s user=%s",
            organization_id,
            user_id or "-",
        )


_resolver: Optional[CredentialResolver] = None
_resolver_built = False


def build_credential_resolver(settings: Any) -> CredentialResolver:
    """Broker when configured, else the static token (local only)."""
    broker_url = (settings.encrypt_tool_internal_url or "").strip()
    broker_secret = (settings.smartsheet_broker_secret or "").strip()
    if broker_url and broker_secret:
        log.info(
            "Smartsheet credentials resolve through smarthub_core against the "
            "OAuth broker at %s (slug=%s)",
            broker_url,
            settings.product_slug,
        )
        return CoreCredentialResolver(settings)

    if not settings.is_local:
        # A single shared token across orgs is not acceptable off a laptop.
        log.error(
            "No OAuth broker configured (ENCRYPT_TOOL_INTERNAL_URL + "
            "SMARTSHEET_BROKER_SECRET) and env=%s is not local — Smartsheet calls "
            "will fail closed",
            settings.app_env,
        )
        return UnconfiguredCredentialResolver(settings.app_env)

    log.warning(
        "Using the service-wide SMARTSHEET_ACCESS_TOKEN (local only) — no per-org OAuth"
    )
    return StaticCredentialResolver((settings.smartsheet_access_token or "").strip())


def get_credential_resolver(settings: Any) -> CredentialResolver:
    global _resolver, _resolver_built
    if not _resolver_built:
        _resolver = build_credential_resolver(settings)
        _resolver_built = True
    assert _resolver is not None
    return _resolver


async def close_credential_resolver() -> None:
    global _resolver, _resolver_built
    _resolver = None
    _resolver_built = False
    from smart_qr.adapters.core_infra import close_core_infra

    await close_core_infra()
