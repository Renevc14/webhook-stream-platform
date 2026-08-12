"""
Smartsheet client scoped by (org_id, product_slug).

Single source of truth for the Smartsheet integration shared across all
Smarthub backends. Resolves credentials via cache-aside on Redis, applies a
sliding-window rate limit per API key, and runs the official Smartsheet SDK
in a thread (the SDK is sync). Retries cover all known transient classes:
gateway errors, HTTP/2 GOAWAY, and Smartsheet's `errorCode 4000` "unexpected
error" — the last one being the production bug observed during backfills
(see `docs/tools/image-to-cell/GAPS_AND_ISSUES.md` issue #16).

## Token resolution

Per `TARGET_ARCHITECTURE.md` §Caching strategy, key 3:

- Key:   `ss-token:{org_id}:{product_slug}`
- Shape: `{"ciphertext": str, "base_url": str}` JSON
- TTL:   600 seconds (`SUPABASE_MAX_KEEPALIVE` semantics — invalidated explicitly on rotation)
- Source of truth: Postgres (`smartsheet_tokens` UNIQUE(org, slug) + `organization_ss_api_base`)
- Fallback on Redis-down: direct read from Postgres + decrypt locally

The token is **always stored as ciphertext in Redis**. Decryption happens
in-process in `_resolve_credentials` and the plaintext never crosses the
Redis boundary.

## Rate limit

Per `TARGET_ARCHITECTURE.md` §Caching strategy, key 6:

- Key:    `ratelimit:ss-api:{org_id}:{product_slug}`
- Window: 60s (Smartsheet's published rate limit window)
- Max:    275 calls/minute (Smartsheet's documented ceiling is 300; we leave headroom)
- Backend: Redis sorted set with Lua atomic ZREMRANGEBYSCORE+ZADD
- Failure mode: fail-open (`rate_limit_consume` returns `(True, 0)` on RedisError)

## Retry

Bug 1 (issue #16 in GAPS_AND_ISSUES.md) — Smartsheet `errorCode 4000` returned
on transient internal errors during high concurrency was not retried by the
legacy client. This module's `call_with_retry_async` includes that pattern.

Bug 6 — HTTP/2 `ConnectionTerminated` from the Supabase HTTP/2 pool also
triggers retry (relevant for any Smartsheet calls that go through PostgREST,
e.g. logging usage in the same operation).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, Optional, Tuple, TypeVar

import smartsheet  # type: ignore[import-untyped]

from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.platforms.shared.token_resolver import TokenNotFoundError
from smarthub_core.platforms.smartsheet.token_resolver import SmartsheetTokenResolver
from smarthub_core.settings import CoreSettings, get_core_settings


log = logging.getLogger(__name__)

T = TypeVar("T")


class SmartsheetError(Exception):
    """Raised when Smartsheet credential resolution or API call fails."""


# Patterns that mark a Smartsheet exception as retryable. Lowercase substring
# match against `str(exception).lower()`. Includes the legacy patterns plus
# the issue-#16 fix (errorCode 4000) and HTTP/2 GOAWAY (issue #21).
RETRYABLE_PATTERNS: frozenset[str] = frozenset(
    {
        # HTTP-level transient
        "500",
        "502",
        "503",
        "504",
        "internal server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        # Smartsheet `errorCode 4000` — the bug observed during backfill (#16)
        '"errorcode": 4000',
        '"errorcode":4000',
        # Hotfix 2026-06-17 (Beth incident): Smartsheet emitted
        # `statusCode:500 errorCode:4004 (UnexpectedErrorShouldRetryError)`
        # in production for /cellimages uploads. The `"500"` substring
        # alone may not appear in every SDK error formatter, so we match
        # the explicit codes and the `shouldRetry` flag.
        #
        # Verified against https://developers.smartsheet.com/api/smartsheet/error-codes:
        # 4004 (500) message reads "Please retry your request" — the official
        # contract instructs retry. 4002 (500, "Server timeout") is covered by
        # the bare "500" / "internal server error" patterns above. 4003 (429,
        # rate limit) intentionally NOT here — the SDK already backs off
        # internally and re-stacking our retry on top compounds an already-
        # exhausted loop. 1004 (403, "Not authorized") and 1018 (400, invalid
        # parameter) are non-retryable by design.
        #
        # Note: the public error codes table does not document a `shouldRetry`
        # field — Smartsheet emits it in the runtime payload only. Matching on
        # `"shouldRetry": true` is defense in depth based on observed CloudWatch
        # payloads, not a documented contract; it future-proofs us against new
        # error codes that adopt the same flag.
        '"errorcode": 4004',
        '"errorcode":4004',
        '"shouldretry": true',
        '"shouldretry":true',
        # SDK internal bug surfacing on 502 — preserved from legacy
        "can only concatenate str",
        # Connection-level (Bug 6 / issue #21 — HTTP/2 GOAWAY)
        "connectionterminated",
        "remoteprotocolerror",
        "server disconnected",
        "remote disconnected",
        "connection reset",
        "connection aborted",
        "broken pipe",
        # Read/timeout
        "timeout",
        "read timed out",
    }
)


# Default rate limit values — calibrated for Smartsheet documented limits
# (300 calls/minute), with 275/min as the safe ceiling.
_RATE_LIMIT_WINDOW_S = 60
_RATE_LIMIT_MAX_CALLS = 275


def is_retryable_smartsheet_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(pat in msg for pat in RETRYABLE_PATTERNS)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SmartsheetClient:
    """Smartsheet API client scoped by `(org_id, product_slug)`.

    The constructor takes both — never resolve credentials with `org_id` alone
    (smartsheet_tokens has UNIQUE(org_id, product_slug); each tool has its own PAT).

    Usage:
        client = SmartsheetClient(
            org_id="...",
            product_slug="smart-image-to-cell",
            redis=rc,
            supabase=svc,
        )
        sheet = await client.call_with_retry_async(
            lambda c: c.Sheets.get_sheet(sheet_id),
            description="get_sheet"
        )
    """

    def __init__(
        self,
        org_id: str,
        product_slug: str,
        *,
        redis: RedisClient,
        supabase: SupabaseService,
        settings: Optional[CoreSettings] = None,
        token_cache_ttl_s: int = 600,
        rate_limit_window_s: int = _RATE_LIMIT_WINDOW_S,
        rate_limit_max_calls: int = _RATE_LIMIT_MAX_CALLS,
        token_resolver: Optional[SmartsheetTokenResolver] = None,
        owner_user_id: Optional[str] = None,
    ) -> None:
        if not org_id or not product_slug:
            raise SmartsheetError("org_id and product_slug are required")

        self._org_id = org_id
        self._slug = product_slug
        # OAuth migration: when set, the resolver asks the encrypt-tool broker
        # for this user's per-tool OAuth token before falling back to the PAT.
        self._owner_user_id = owner_user_id
        self._redis = redis
        self._supabase = supabase
        self._settings = settings or get_core_settings()

        self._token_cache_ttl_s = token_cache_ttl_s
        self._rate_limit_window_s = rate_limit_window_s
        self._rate_limit_max_calls = rate_limit_max_calls

        self._token_key = SmartsheetTokenResolver.cache_key(org_id, product_slug)
        self._rate_limit_key = f"ratelimit:ss-api:{org_id}:{product_slug}"

        # Token resolution is delegated to SmartsheetTokenResolver (single
        # source of truth for credential resolution across the platform). The
        # client owns the SDK-client cache + rate limit + retry; the resolver
        # owns the cache-aside on Redis and the Postgres reads.
        self._token_resolver = token_resolver or SmartsheetTokenResolver(
            redis=redis,
            supabase=supabase,
            settings=self._settings,
            cache_ttl_s=token_cache_ttl_s,
        )

        # Lazy: resolved on first call_with_retry_async / get_client.
        self._cached_sdk_client: Optional[smartsheet.Smartsheet] = None
        self._cached_token: Optional[str] = None
        self._cached_base_url: Optional[str] = None
        self._init_lock = asyncio.Lock()

    @property
    def org_id(self) -> str:
        return self._org_id

    @property
    def product_slug(self) -> str:
        return self._slug

    # ----- credential resolution ------------------------------------------

    async def _resolve_credentials(self, *, force_refresh: bool = False) -> Tuple[str, str]:
        """Return (plaintext_token, base_url). Delegates to the resolver.

        The shared ``SmartsheetTokenResolver`` owns the cache-aside +
        Postgres read + decrypt logic so there's a single source of truth for
        Smartsheet credential resolution across the platform.
        """
        try:
            resolved = await self._token_resolver.resolve(
                self._org_id,
                self._slug,
                force_refresh=force_refresh,
                user_id=self._owner_user_id,
            )
        except TokenNotFoundError as e:
            raise SmartsheetError(str(e)) from e
        return resolved.access_token, resolved.base_url

    async def get_client(self, *, force_refresh: bool = False) -> smartsheet.Smartsheet:
        """Return a configured Smartsheet SDK client. Cached after first build."""
        async with self._init_lock:
            if self._cached_sdk_client is not None and not force_refresh:
                return self._cached_sdk_client

            token, base_url = await self._resolve_credentials(force_refresh=force_refresh)

            sdk_client = await asyncio.to_thread(_build_sdk_client, token, base_url)
            self._cached_sdk_client = sdk_client
            self._cached_token = token
            self._cached_base_url = base_url
            return sdk_client

    # ----- rate limit -----------------------------------------------------

    async def _wait_for_rate_limit_slot(self, *, max_wait_s: float = 30.0) -> None:
        """Block until a rate-limit slot is available, up to `max_wait_s`."""
        deadline = time.monotonic() + max_wait_s
        backoff = 0.05
        while True:
            allowed, _count = await self._redis.rate_limit_consume(
                self._rate_limit_key,
                window_s=self._rate_limit_window_s,
                max_calls=self._rate_limit_max_calls,
            )
            if allowed:
                return
            if time.monotonic() >= deadline:
                raise SmartsheetError(
                    f"Rate limit exhausted for org={self._org_id} slug={self._slug} "
                    f"after waiting {max_wait_s}s"
                )
            jitter = random.uniform(0, backoff * 0.3)
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2, 1.0)

    # ----- call with retry ------------------------------------------------

    async def call_with_retry_async(
        self,
        operation: Callable[[smartsheet.Smartsheet], T],
        *,
        description: str = "",
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> T:
        """Run `operation(smartsheet_sdk_client)` with rate-limit + retry.

        The operation is a sync callable that receives the configured Smartsheet
        SDK client. It runs in a thread so the event loop stays free.

        Retries cover all patterns in `RETRYABLE_PATTERNS` — including the
        new errorCode 4000 (Bug 1) and HTTP/2 GOAWAY (Bug 6) cases.

        On 401 (token revoked / rotated mid-flight), the cached SDK client is
        invalidated and the operation is retried with a freshly-resolved token.
        """
        last_error: Optional[BaseException] = None

        for attempt in range(max_retries + 1):
            try:
                await self._wait_for_rate_limit_slot()
                sdk_client = await self.get_client()
                return await asyncio.to_thread(operation, sdk_client)

            except Exception as e:
                last_error = e
                msg = str(e).lower()

                # Stale-token branch: token was rotated and our cached SDK client
                # is wrong. Force-refresh credentials and retry once outside the
                # retry budget. We treat this as a recoverable case independent
                # of `RETRYABLE_PATTERNS`.
                if "401" in msg or "unauthorized" in msg or "invalid token" in msg:
                    log.info(
                        "smartsheet: stale token detected (org=%s slug=%s) — refreshing",
                        self._org_id,
                        self._slug,
                    )
                    await self.invalidate_credentials_cache()
                    if attempt < max_retries:
                        continue
                    raise SmartsheetError(f"{description} failed after refresh: {e}") from e

                if not is_retryable_smartsheet_error(e) or attempt == max_retries:
                    log.error("smartsheet: %s failed: %s", description, e)
                    raise SmartsheetError(f"{description} failed: {e}") from e

                delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
                log.warning(
                    "smartsheet: %s transient error (attempt %d/%d), retry in %.1fs: %s",
                    description,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

        raise SmartsheetError(
            f"{description} failed after {max_retries} retries: {last_error}"
        )

    async def invalidate_credentials_cache(self) -> None:
        """Drop the cached Smartsheet client + Redis cache entry. Forces a
        fresh DB read on next call. Use after token rotation.
        """
        async with self._init_lock:
            self._cached_sdk_client = None
            self._cached_token = None
            self._cached_base_url = None
        await self._token_resolver.invalidate(self._org_id, self._slug)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_sdk_client(token: str, base_url: str) -> smartsheet.Smartsheet:
    """Construct a Smartsheet SDK client. Tolerates SDK-version differences
    in how the API base is set."""
    try:
        client = smartsheet.Smartsheet(token, api_base=base_url)
    except TypeError:
        # Older SDK does not accept api_base in constructor
        client = smartsheet.Smartsheet(token)
        _set_base_url(client, base_url)
    client.errors_as_exceptions(True)
    return client


def _set_base_url(client: Any, base_url: str) -> None:
    """Best-effort set of the API base on different SDK versions."""
    candidates = [client] + list(getattr(client, "__dict__", {}).values())
    for obj in candidates:
        for attr in ("_api_base", "_base_url", "base_url"):
            if hasattr(obj, attr):
                try:
                    setattr(obj, attr, base_url)
                    return
                except Exception:
                    pass
