"""OAuth refresh-token helpers shared across providers.

Long-lived PAT providers (Smartsheet, Stripe API key, Twilio account token)
do not need this module. Providers with OAuth access tokens that expire
(HubSpot's ~6h, Slack's rare rotation, future Google/Microsoft integrations)
implement ``OAuthRefresher`` and reuse the ``should_refresh`` heuristic and
the single-flight refresh lock pattern documented here.

## Single-flight refresh

Without coordination, multiple workers seeing an expiring token would each
hit the provider's ``/token`` endpoint at the same time, wasting refresh
budget and possibly triggering rate limits. The pattern is:

1. ``acquire_lock("oauth-refresh:{provider}:{org}:{slug}", ttl_s=30)``
2. Re-read the token from cache or DB. If a sibling already refreshed it,
   return that result.
3. Call the provider's refresh endpoint with the refresh token.
4. ``UPDATE {provider}_tokens SET encrypted_access_token=?, expires_at=?``
5. ``DEL {provider}-token:{org}:{slug}`` cache key.
6. Release the lock.

If lock acquisition fails (sibling holds it): poll the cache for ~2s — the
holder will refresh and DEL the cache; the next read repopulates it. If the
sibling crashes mid-refresh, the lock TTL releases the contention and the
next caller retries.

## Refresh threshold

Providers usually issue tokens with 1-6h lifetimes. We refresh proactively
when ``expires_at - now() < refresh_threshold``. Default is 5 minutes:
- Long enough to cover clock skew + network jitter on the refresh request
- Short enough that we don't waste refreshes for tokens that won't be reused
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional


DEFAULT_REFRESH_THRESHOLD = timedelta(minutes=5)


class OAuthRefreshError(Exception):
    """Raised when the provider rejects a refresh request.

    Distinguished from transient network errors so callers can mark the
    integration as needing re-auth (refresh token revoked, scope changed,
    ...) rather than retrying."""


@dataclass(frozen=True)
class RefreshedToken:
    """Result of a successful refresh.

    Most providers return a new ``access_token`` and a new ``expires_at``,
    keeping the same ``refresh_token``. Some providers (Google) rotate the
    refresh token on every refresh — ``refresh_token`` is the new value when
    that's the case, otherwise ``None``.
    """

    access_token: str
    expires_at: datetime
    refresh_token: Optional[str] = None
    scope: Optional[str] = None
    metadata: Mapping[str, str] = ()  # type: ignore[assignment]


class OAuthRefresher(ABC):
    """Provider-agnostic OAuth refresh interface.

    One impl per OAuth provider (``HubSpotOAuthRefresher``,
    ``GoogleOAuthRefresher``, ...). The shared module provides this contract
    plus the ``should_refresh`` helper; the impl just calls the provider's
    token endpoint and parses the response.
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """Lowercase provider identifier."""

    @abstractmethod
    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> RefreshedToken:
        """Exchange ``refresh_token`` for a fresh access token.

        Raises:
            OAuthRefreshError: provider rejected the refresh (revoked, expired,
                               scope mismatch, ...). Caller should mark the
                               integration as needing re-auth.
            Exception:         transient network errors propagate so the
                               caller can retry under its own policy.
        """


def should_refresh(
    expires_at: Optional[datetime],
    *,
    threshold: timedelta = DEFAULT_REFRESH_THRESHOLD,
    now: Optional[datetime] = None,
) -> bool:
    """Return ``True`` when the token expires within ``threshold``.

    ``expires_at=None`` means a long-lived token — never needs refresh.
    Naive datetimes are assumed to be UTC."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (expires_at - current) < threshold
