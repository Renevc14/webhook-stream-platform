"""Provider-agnostic token resolution.

Defines the ``TokenResolver`` interface that every provider implements
(``platforms/{provider}/token_resolver.py``). The Webhook Gateway, Lifecycle
Manager, and admin tooling depend on this interface, not on provider-specific
classes — that decoupling is what lets a new provider plug in without touching
the core.

## Contract

Each provider stores its tokens in its own table
(``smartsheet_tokens``, ``hubspot_tokens``, ``slack_tokens``, ...) with a
``UNIQUE(organization_id, product_slug)`` constraint. The resolver hides that
detail and exposes a single ``resolve(org_id, product_slug) -> ResolvedToken``
call.

Cache key conventions (each impl owns its key namespace, but the shape is
uniform):

- Smartsheet: ``ss-token:{org}:{slug}``
- HubSpot:    ``hs-token:{org}:{slug}``
- Slack:      ``slack-token:{org}:{slug}``

Tokens are **always cached as ciphertext**. Decryption happens in-process
inside the resolver, never inside Redis.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional


class TokenNotFoundError(Exception):
    """Raised when no token row exists for ``(org_id, product_slug)`` in the
    provider's table. Distinguished from transient errors so callers can
    decide whether to surface "not configured" vs "infrastructure failure"."""


class TokenRevokedError(TokenNotFoundError):
    """Raised when the credential broker authoritatively reports that the
    user's OAuth grant is revoked (or terminally failed) and no transition
    fallback credential exists (HTTP 409 from ``/internal/token/resolve``).

    Subclasses ``TokenNotFoundError`` so existing callers keep their
    "needs reconnect" handling, while consumers that drive runner failover
    (SH-178) can catch this type specifically and read ``oauth_state`` /
    ``reason`` instead of guessing from a generic no-credentials error.

    Never carries token material.
    """

    def __init__(self, message: str, *, oauth_state: str = "revoked",
                 reason: str = "CONNECTION_REVOKED") -> None:
        super().__init__(message)
        self.oauth_state = oauth_state
        self.reason = reason


class TokenTemporarilyUnavailableError(TokenNotFoundError):
    """Raised when the credential broker could not answer (transport error,
    5xx, or a contract-violating 200) and no local fallback credential exists.

    Distinct from a plain ``TokenNotFoundError`` because the two demand
    opposite responses: "not found" means the user must connect the tool,
    while this means *we* could not ask right now and the caller should retry.
    Conflating them tells a correctly-connected user to reconnect, and can mark
    a retryable unit of work as permanently failed.

    Subclasses ``TokenNotFoundError`` so callers that only distinguish
    "no credentials" from infra errors keep working unchanged.
    """


@dataclass(frozen=True)
class ResolvedToken:
    """Decrypted token + provider-specific metadata returned by a resolver.

    Fields:
      access_token:  Plaintext bearer token / API key. Caller must not log,
                     persist, or pass it through Redis.
      base_url:      Provider-region API base when applicable (Smartsheet US/EU/
                     Gov, Stripe livemode vs test, etc.). ``None`` for providers
                     with a single global endpoint.
      refresh_token: Plaintext refresh token for OAuth providers. ``None`` for
                     long-lived PAT/API-key providers.
      expires_at:    Absolute expiry for OAuth access tokens. ``None`` for
                     long-lived tokens that don't expire.
      metadata:      Provider-specific extras (account_id, team_id, scope, ...).
    """

    access_token: str
    base_url: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TokenResolver(ABC):
    """Provider-agnostic token resolution interface.

    Each provider impl (``SmartsheetTokenResolver``, ``HubSpotTokenResolver``,
    ...) follows the same shape so callers can resolve credentials without
    knowing which provider they're talking to.

    Implementations are responsible for:

    1. **Cache-aside on Redis** keyed by ``(org_id, product_slug)``. Ciphertext
       only — never plaintext.
    2. **Reading from Postgres on miss** through ``execute_with_retry_async``.
    3. **Decrypting in-process** via ``smarthub_core.crypto.decrypt_token``.
    4. **Refreshing** when the provider uses OAuth and the access token is
       within the refresh threshold (see ``smarthub_core.platforms.shared.oauth``).
    5. **Raising ``TokenNotFoundError``** when no row exists, distinct from
       transient infra errors.
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """Lowercase provider identifier — ``"smartsheet"``, ``"hubspot"``,
        ``"slack"``, ``"stripe"``, ``"twilio"``, ..."""

    @abstractmethod
    async def resolve(
        self,
        org_id: str,
        product_slug: str,
        *,
        force_refresh: bool = False,
    ) -> ResolvedToken:
        """Resolve credentials for ``(org_id, product_slug)``.

        ``force_refresh=True`` skips the Redis cache and reads directly from
        Postgres — used after token rotation or 401/refresh-token flows.

        Raises:
            TokenNotFoundError: no row in the provider's tokens table.
            Exception:          transient or fatal infra errors propagate so
                                the caller can apply its retry policy.
        """

    @abstractmethod
    async def invalidate(self, org_id: str, product_slug: str) -> None:
        """Delete the Redis cache entry for this ``(org_id, product_slug)``.

        Idempotent. Always called after a successful token rotation, after
        OAuth refresh, and on 401 in a long-lived path."""
