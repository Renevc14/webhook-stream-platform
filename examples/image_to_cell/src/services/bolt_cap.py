"""
Bolt cap for Image-to-Cell: subscription, hard-cap, pricing_list.primary_event_bolts,
and mid-job remaining image capacity.

SH-222 — consumed baseline comes from Hub RPC ``get_org_billing_usage_total``
(same org-wide seed SmartQR uses via ``smarthub_core.billing``). Shared Redis
keys ``bolt-cap:{org_id}:*`` must not be seeded from I2C-only event-log counts.

SH-103 (part 2) — cluster-wide atomic enforcement:

    The legacy path here (``get_remaining_image_capacity``) reads from a 30s
    per-process cache. Under burst load N workers each see the same
    ``remaining=X`` and collectively overshoot the cap before the first one
    writes to ``imagetocell_usage_log``. The new
    :func:`atomic_reserve_bolts` async helper sits on top of
    ``smarthub_core.db.redis.RedisClient.atomic_deduct`` and serialises the
    read+write inside a single Lua execution under Redis keys
    ``bolt-cap:{org_id}:consumed`` and ``bolt-cap:{org_id}:limit``. When
    Redis is unavailable the helper falls back to the legacy per-process
    check so the service keeps running (degraded — TOCTOU window reopens
    but at least nothing is hard-blocked).
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
import uuid
from typing import Any, Dict, Optional, Tuple

from src.services.supabase.client import get_supabase_service

log = logging.getLogger(__name__)

PRODUCT_SLUG = "smart-image-to-cell"
DEFAULT_BOLTS_PER_IMAGE = 15

# Short-lived cache to prevent thundering-herd during concurrent backfill.
# Caching for 30 s is safe because cap enforcement only needs cycle-level precision.
_USAGE_CACHE_TTL_SECONDS = 30
_usage_cache: Dict[str, Any] = {}
_usage_cache_lock = threading.Lock()

# Redis key prefixes for SH-103 atomic enforcement. Stable + namespaced
# so operations dashboards can grep for them.
_REDIS_CONSUMED_PREFIX = "bolt-cap:{org_id}:consumed"
_REDIS_LIMIT_PREFIX = "bolt-cap:{org_id}:limit"
# SH-135: cycle identifier kept alongside the consumed counter so we can
# detect billing-cycle rollover atomically and reset consumed to the new
# cycle's baseline without a separate operator action.
_REDIS_CYCLE_ID_PREFIX = "bolt-cap:{org_id}:cycle_id"
# TTL for the cached limit. The consumed counter is intentionally not
# TTL'd — it persists across the billing cycle.
_REDIS_LIMIT_TTL_S = 60
# SH-150: single-flight lock while refreshing the Postgres-backed usage
# snapshot on cache miss. Short TTL — only covers the fetch window.
_FETCH_LOCK_PREFIX = "lock:fetch:bolt-cap:"
_FETCH_LOCK_TTL_S = 30
_FETCH_LOCK_WAIT_S = 2.0
_FETCH_LOCK_POLL_S = 0.1


def _first_row(data: Any) -> Optional[Dict[str, Any]]:
    if data is None:
        return None
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else None
    if isinstance(data, dict):
        return data
    return None


def _org_billing_usage_total(
    db: Any,
    organization_id: str,
) -> Optional[int]:
    """Org-wide current-cycle bolts via Hub RPC (SH-222).

    Same baseline SmartQR uses through ``smarthub_core.billing``. Replaces the
    I2C-only ``imagetocell_usage_log`` seed that could diverge under shared
    Redis ``bolt-cap:{org}:*`` keys.
    """
    try:
        result = db.client.rpc(
            "get_org_billing_usage_total",
            {"p_organization_id": organization_id},
        ).execute()
    except Exception as e:
        log.warning(
            "Bolt cap: get_org_billing_usage_total failed org=%s: %s",
            organization_id,
            e,
        )
        return None

    data = result.data
    if data is None:
        return None
    if isinstance(data, (int, float)):
        return max(0, int(data))
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, (int, float)):
            return max(0, int(row))
        if isinstance(row, dict):
            for key in (
                "get_org_billing_usage_total",
                "total",
                "bolts",
                "consumed",
            ):
                if row.get(key) is not None:
                    return max(0, int(row[key]))
            # Single-value dict fallback
            if len(row) == 1:
                return max(0, int(next(iter(row.values()))))
    if isinstance(data, dict):
        for key in ("total", "bolts", "consumed"):
            if data.get(key) is not None:
                return max(0, int(data[key]))
    log.warning(
        "Bolt cap: unexpected get_org_billing_usage_total payload org=%s data=%r",
        organization_id,
        data,
    )
    return None


def _count_i2c_images_in_cycle(
    db: Any,
    organization_id: str,
    cycle_start: Any,
) -> int:
    """Deprecated I2C-only event-log count — kept for diagnostics only.

    SH-222: enforcement baseline is :func:`_org_billing_usage_total`. Do not
    use this to seed Redis.
    """
    if not cycle_start:
        return 0
    try:
        result = (
            db.table("imagetocell_usage_log")
            .select("id", count="exact")
            .eq("organization_id", organization_id)
            .eq("success", True)
            .gte("created_at", cycle_start)
            .limit(1)
            .execute()
        )
        return int(getattr(result, "count", 0) or 0)
    except Exception as e:
        log.warning(
            "Bolt cap: event-log count failed org=%s: %s", organization_id, e
        )
        return 0


def compute_effective_remaining_bolts(
    sub: Dict[str, Any],
    hard_cap_pct: Optional[float],
    plan_type: Optional[str],
    allocated: int,
    consumed: int,
) -> Optional[int]:
    """Bolts the org can still spend this cycle under cap (same logic as attachment-counter)."""
    if allocated < 0 or consumed < 0:
        return None
    is_free = (plan_type or "").lower() == "individual"
    remaining_rpc = sub.get("total_bolts_remaining")
    if remaining_rpc is None:
        remaining_rpc = sub.get("current_cycle_total_bolts_remaining")
    if remaining_rpc is None:
        remaining_rpc = sub.get("bolts_remaining")

    if is_free and allocated > 0:
        return max(0, allocated - consumed)
    if hard_cap_pct is not None and hard_cap_pct > 0 and allocated > 0:
        cap = int((allocated * hard_cap_pct) / 100.0)
        return max(0, cap - consumed)
    if remaining_rpc is not None:
        try:
            return max(0, int(remaining_rpc))
        except (TypeError, ValueError):
            pass
    return max(0, allocated - consumed)


def _load_i2c_org_usage_context_inner(organization_id: str) -> Optional[Dict[str, Any]]:
    """Single attempt — raises on any failure so the caller can retry."""
    db = get_supabase_service()

    sub_result = db.client.rpc(
        "get_subscription_status", {"p_organization_id": organization_id}
    ).execute()
    sub = _first_row(sub_result.data) or {}
    if not sub:
        log.warning("Bolt cap: get_subscription_status returned no data for org")
        return None

    allocated = int(
        sub.get("current_cycle_total_bolts_allocated")
        or sub.get("total_bolts_allocated")
        or 0
    )
    consumed_sub = int(
        sub.get("current_cycle_total_bolts_consumed")
        or sub.get("total_bolts_consumed")
        or 0
    )

    try:
        org_res = (
            db.table("organization_subscriptions")
            .select("hard_cap_percentage, plan_suspended, bolts_pending_payment")
            .eq("organization_id", organization_id)
            .limit(1)
            .execute()
        )
        org_row = (org_res.data[0] if org_res.data else {}) or {}
    except Exception as e:
        log.warning("Bolt cap: organization_subscriptions read failed org=%s: %s", organization_id, e)
        org_row = {}

    plan_type: Optional[str] = None
    try:
        ores = db.table("organizations").select("plan_type").eq("id", organization_id).limit(1).execute()
        if ores.data:
            plan_type = (ores.data[0].get("plan_type") or "").lower() or None
    except Exception as e:
        log.warning("Bolt cap: organizations read failed org=%s: %s", organization_id, e)

    is_free = plan_type == "individual"

    hcp = org_row.get("hard_cap_percentage")
    hard_cap_pct: Optional[float] = None
    if hcp is not None:
        try:
            hard_cap_pct = float(hcp)
        except (TypeError, ValueError):
            hard_cap_pct = None

    ceiling: Optional[int] = None
    # Prefer Hub hard_cap_bolt_amount (same as smarthub_core.billing) when set.
    hard_cap_amount_raw = sub.get("hard_cap_bolt_amount")
    try:
        hard_cap_amount = int(hard_cap_amount_raw) if hard_cap_amount_raw is not None else 0
    except (TypeError, ValueError):
        hard_cap_amount = 0
    if hard_cap_amount > 0:
        ceiling = hard_cap_amount
    elif allocated > 0 and is_free:
        ceiling = allocated
    elif allocated > 0 and hard_cap_pct is not None and hard_cap_pct > 0:
        ceiling = int((allocated * hard_cap_pct) / 100.0)

    pricing_row = (
        db.table("pricing_list")
        .select("primary_event_bolts")
        .eq("product_slug", PRODUCT_SLUG)
        .limit(1)
        .execute()
    )
    bolts_per_image = DEFAULT_BOLTS_PER_IMAGE
    if pricing_row.data:
        bpi = pricing_row.data[0].get("primary_event_bolts")
        if bpi is not None:
            bolts_per_image = int(bpi)
    if bolts_per_image <= 0:
        bolts_per_image = DEFAULT_BOLTS_PER_IMAGE

    cycle_start = sub.get("current_cycle_start")
    # SH-222: org-wide usage RPC — same seed SmartQR uses. Do not max() with
    # the I2C-only event-log count (that under-seeds shared Redis keys).
    usage_total = _org_billing_usage_total(db, organization_id)
    if usage_total is not None:
        consumed = usage_total
    else:
        # Soft fallback: subscription counter only (still org-facing fields,
        # not I2C event-log). Log loudly so ops can see baseline degradation.
        consumed = consumed_sub
        log.error(
            "Bolt cap: usage RPC unavailable org=%s — falling back to "
            "subscription consumed=%s (may diverge from SmartQR until RPC recovers)",
            organization_id,
            consumed_sub,
        )

    remaining_eff = compute_effective_remaining_bolts(
        sub, hard_cap_pct, plan_type, allocated, consumed
    )

    return {
        "plan_suspended": bool(org_row.get("plan_suspended")),
        "bolts_pending_payment": bool(org_row.get("bolts_pending_payment")),
        "hard_cap_reached_rpc": bool(sub.get("hard_cap_reached")),
        "hard_cap_percentage": hard_cap_pct,
        "plan_type": plan_type,
        "is_free_plan": is_free,
        "allocated_bolts": allocated,
        "consumed_bolts_start": consumed,
        "ceiling_bolts": ceiling,
        "remaining_effective_bolts": remaining_eff,
        "bolts_per_image": bolts_per_image,
        # SH-135: cycle identifier surfaced for the atomic rollover detection
        # in `atomic_reserve_bolts`. May be ``None`` if the subscription RPC
        # did not return one — callers must handle that case.
        "current_cycle_start": cycle_start,
    }


def _load_i2c_org_usage_context_with_retry(
    organization_id: str,
) -> Optional[Dict[str, Any]]:
    """Fetch org usage context from Postgres with retry/backoff."""
    from src.utils.safe_log import safe_exception_message

    max_attempts = 3
    delay = 1.0
    result: Optional[Dict[str, Any]] = None
    for attempt in range(max_attempts):
        try:
            result = _load_i2c_org_usage_context_inner(organization_id)
            break
        except Exception as e:
            if attempt < max_attempts - 1:
                log.warning(
                    "load_i2c_org_usage_context attempt %d/%d failed: %s — retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    safe_exception_message(e),
                    delay,
                )
                time.sleep(delay)
                delay *= 2.0
            else:
                log.error(
                    "load_i2c_org_usage_context failed after %d attempts: %s",
                    max_attempts,
                    safe_exception_message(e),
                )
    return result


def _read_cached_usage_context(organization_id: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _usage_cache_lock:
        entry = _usage_cache.get(organization_id)
        if entry and (now - entry["ts"]) < _USAGE_CACHE_TTL_SECONDS:
            return entry["data"]
    return None


def _store_usage_context_cache(
    organization_id: str,
    result: Optional[Dict[str, Any]],
) -> None:
    with _usage_cache_lock:
        _usage_cache[organization_id] = {"ts": time.monotonic(), "data": result}


def load_i2c_org_usage_context(organization_id: str) -> Optional[Dict[str, Any]]:
    """
    Load org subscription snapshot + bolts_per_image for gating and capacity.

    Results are cached for 30 s to prevent a thundering herd during concurrent
    backfill (many row workers firing 5 Supabase calls each for the same org).
    Each attempt retries up to 3 times with exponential backoff so transient
    RemoteProtocolError / server-disconnected errors don't abort processing.

    Returns None on unexpected failure (callers may fail-open).
    """
    cached = _read_cached_usage_context(organization_id)
    if cached is not None:
        return cached

    result = _load_i2c_org_usage_context_with_retry(organization_id)
    _store_usage_context_cache(organization_id, result)
    return result


async def load_i2c_org_usage_context_async(
    organization_id: str,
) -> Optional[Dict[str, Any]]:
    """Async loader with cross-replica stampede lock (SH-150).

    On cache miss, only one worker per org refreshes from Postgres; others
    poll the in-process cache briefly, then fall back to the sync loader.
    """
    cached = _read_cached_usage_context(organization_id)
    if cached is not None:
        return cached

    from src.services.redis_client import get_redis_client

    redis_client = get_redis_client()
    if redis_client is None:
        log.warning(
            "bolt_cap: redis not wired — sync usage fetch org=%s",
            organization_id,
        )
        return load_i2c_org_usage_context(organization_id)

    lock_name = f"{_FETCH_LOCK_PREFIX}{organization_id}"
    token = uuid.uuid4().hex
    acquired = await redis_client.acquire_lock(
        lock_name, token, _FETCH_LOCK_TTL_S,
    )
    if acquired:
        try:
            cached = _read_cached_usage_context(organization_id)
            if cached is not None:
                return cached
            result = await asyncio.to_thread(
                _load_i2c_org_usage_context_with_retry,
                organization_id,
            )
            _store_usage_context_cache(organization_id, result)
            return result
        finally:
            released = await redis_client.release_lock(lock_name, token)
            if not released:
                log.warning(
                    "bolt_cap: fetch lock release failed org=%s lock=%s",
                    organization_id,
                    lock_name,
                )

    log.info(
        "bolt_cap: fetch lock busy — waiting for peer refresh org=%s",
        organization_id,
    )
    deadline = time.monotonic() + _FETCH_LOCK_WAIT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_FETCH_LOCK_POLL_S)
        cached = _read_cached_usage_context(organization_id)
        if cached is not None:
            return cached

    log.warning(
        "bolt_cap: fetch lock wait timed out org=%s — loading directly",
        organization_id,
    )
    return load_i2c_org_usage_context(organization_id)


def get_remaining_image_capacity(organization_id: str) -> Tuple[int, Optional[str]]:
    """
    How many more images can be processed before hitting the bolt cap / blocks.

    Returns (remaining_images, error_message). 0 = stop; error_message explains why.
    """
    ctx = load_i2c_org_usage_context(organization_id)
    if ctx is None:
        return 0, "Subscription status unavailable"

    if ctx.get("plan_suspended"):
        return 0, "Plan suspended"
    if ctx.get("bolts_pending_payment"):
        return 0, "Bolts pending payment"
    if ctx.get("hard_cap_reached_rpc"):
        return 0, "Hard cap reached"

    ceiling = ctx.get("ceiling_bolts")
    consumed_start = int(ctx.get("consumed_bolts_start") or 0)
    if ceiling is not None and consumed_start >= ceiling:
        return 0, "Hard cap reached" if ctx.get("hard_cap_percentage") else "Free plan limit reached"

    remaining_eff = ctx.get("remaining_effective_bolts")
    bolts_per_image = int(ctx.get("bolts_per_image") or DEFAULT_BOLTS_PER_IMAGE)
    if bolts_per_image < 1:
        bolts_per_image = DEFAULT_BOLTS_PER_IMAGE

    if remaining_eff is None:
        remaining_eff = 0

    if remaining_eff < bolts_per_image:
        is_free = ctx.get("is_free_plan")
        hcp = ctx.get("hard_cap_percentage")
        if is_free:
            reason = "Free plan limit reached"
        elif hcp:
            reason = "Hard cap reached"
        else:
            reason = "Insufficient bolts for one image"
        log.info(
            "Bolt cap: org blocked remaining_eff=%s bolts_per_image=%s reason=%s",
            remaining_eff,
            bolts_per_image,
            reason,
        )
        return 0, reason

    remaining_images = remaining_eff // bolts_per_image
    return remaining_images, None


def can_process_one_more_image(organization_id: str) -> bool:
    remaining, _ = get_remaining_image_capacity(organization_id)
    return remaining >= 1


async def atomic_reserve_bolts(
    organization_id: str,
    n_images: int,
) -> Tuple[bool, int]:
    """Atomic check-and-reserve against this org's bolt cap (SH-103).

    Returns ``(allowed, headroom)``:

      - ``allowed=True`` means the org has budget for ``n_images`` and the
        Redis consumed counter has been incremented by ``n_images *
        bolts_per_image``. The caller may proceed with image processing.
      - ``allowed=False`` means the org would overshoot the cap. The
        consumed counter is untouched. ``headroom`` is the number of
        BOLTS still available (not images) — when ``-1`` the cap is
        unknown (default-open path).

    When Redis is unavailable (no client wired, or transient ``RedisError``
    / :class:`RedisHelperError` from ``atomic_deduct*``) this falls back to
    the legacy per-process check :func:`get_remaining_image_capacity` and
    returns based on its result.
    The fallback re-introduces the TOCTOU window the helper closes — so
    in healthy operation we want Redis up.

    The consumed counter is **not** TTL'd. Source of truth for billing
    remains ``imagetocell_usage_log`` in Postgres. The Redis counter is a
    fast-path enforcement cache.

    Billing-cycle rollover is detected automatically (SH-135). A sibling
    key ``bolt-cap:{org}:cycle_id`` carries the last seen
    ``current_cycle_start`` (from ``subscription_status``). When the
    expected cycle id differs from the cached one, the Lua script
    atomically resets the consumed counter to ``consumed_bolts_start``
    (the baseline reported by Postgres for the new cycle) before
    evaluating the deduct — so the first request of a new cycle sees a
    fresh counter, no operator intervention needed. When the
    subscription RPC does not yield a cycle id (no active subscription,
    cold start race, etc.) the helper falls back to the cycle-less
    ``atomic_deduct`` and seeds with the source-of-truth baseline.

    Failure mode contract is intentionally diametrically opposite to
    :func:`smarthub_core.db.redis.RedisClient.rate_limit_consume`:

      - Missing limit (cache eviction) → default-open. Treats the cap as
        ``+inf`` and allows. Operators can populate the limit
        explicitly by calling this function (it seeds the limit cache
        on every call).
      - ``RedisError`` mid-call → fail-closed (helper returns
        ``(False, 0)``). Letting a caller proceed unmetered on a Redis
        hiccup is exactly the customer-visible double-bill that the
        higher-layer fixes (SH-98 / SH-108) closed at the DB + secret
        layers. **Transport errors** (including stale Lua SHA after Redis
        restart) now raise :class:`RedisHelperError` so this function can
        fall back to the legacy Postgres peek instead of falsely denying.
    """
    if n_images <= 0:
        # Defensive: callers pass len(to_process); make a zero-batch a
        # trivial allow rather than letting the helper raise.
        return True, -1

    # Lazy imports to keep the module load cost down for the sync code path.
    from src.services.redis_client import get_redis_client

    redis_client = get_redis_client()
    ctx = await load_i2c_org_usage_context_async(organization_id)
    if ctx is None:
        # Never invent a price or default-open when Hub budget is unknown.
        log.error(
            "bolt_cap: no usage context org=%s — deny reserve (fail-closed)",
            organization_id,
        )
        return False, 0

    bolts_per_image = int(ctx.get("bolts_per_image") or DEFAULT_BOLTS_PER_IMAGE)
    if bolts_per_image < 1:
        bolts_per_image = DEFAULT_BOLTS_PER_IMAGE
    requested_bolts = n_images * bolts_per_image

    # Fallback when Redis is not wired. Outside local this is fail-closed
    # (no legacy peek) so staging/prod cannot overspend a shared Postgres
    # snapshot across tasks. Local may keep the peek for unit tests /
    # dockerless development.
    if redis_client is None:
        from src.config.settings import get_settings

        try:
            is_local = bool(get_settings().is_local)
        except Exception:  # noqa: BLE001
            is_local = False
        if not is_local:
            log.error(
                "bolt_cap: Redis not wired org=%s — deny reserve (fail-closed)",
                organization_id,
            )
            return False, 0
        return _legacy_check_for_images(organization_id, n_images, bolts_per_image)

    consumed_key = _REDIS_CONSUMED_PREFIX.format(org_id=organization_id)
    limit_key = _REDIS_LIMIT_PREFIX.format(org_id=organization_id)
    cycle_id_key = _REDIS_CYCLE_ID_PREFIX.format(org_id=organization_id)

    # Compute the ceiling for this cycle from the (cached) context. Setting
    # the limit key on every call keeps the TTL'd cache fresh in Redis
    # without an extra round-trip per request.
    ceiling = _ceiling_bolts_from_context(ctx)
    try:
        if ceiling is not None and ceiling >= 0:
            await redis_client.raw.set(
                limit_key, str(int(ceiling)), ex=_REDIS_LIMIT_TTL_S,
            )
        else:
            # Unknown limit → delete so atomic_deduct hits the default-open
            # path rather than serving a stale value.
            await redis_client.raw.delete(limit_key)
    except Exception as e:
        # If even the limit-key write fails the installed client is not a
        # real one (most likely a MagicMock from unit tests). Fall back to
        # the legacy peek so the service keeps working.
        log.debug(
            "bolt_cap: SH-103 Redis surface unavailable org=%s err=%s — "
            "using legacy peek",
            organization_id, e,
        )
        return _legacy_check_for_images(organization_id, n_images, bolts_per_image)

    # SH-135: cycle-aware path when we have a cycle identifier from the
    # subscription RPC. The Lua script atomically detects rollover
    # (cached cycle_id ≠ expected cycle_id) and resets the consumed
    # counter to `consumed_bolts_start` (Postgres-known baseline for the
    # new cycle) before evaluating the deduct.
    cycle_id = ctx.get("current_cycle_start") if ctx else None
    cycle_baseline = int(ctx.get("consumed_bolts_start") or 0) if ctx else 0

    try:
        if cycle_id:
            allowed, remaining = await redis_client.atomic_deduct_with_cycle(
                consumed_key,
                limit_key,
                cycle_id_key,
                requested_bolts,
                str(cycle_id),
                cycle_baseline,
            )
        else:
            # No cycle identifier available (RPC returned nothing, or
            # subscription has no period yet). Fall back to the non-cycle
            # variant but still seed the consumed counter from the
            # source-of-truth baseline so we are not double-counting.
            await redis_client.raw.set(consumed_key, str(cycle_baseline), nx=True)
            allowed, remaining = await redis_client.atomic_deduct(
                consumed_key, limit_key, requested_bolts,
            )
    except Exception as e:
        log.debug(
            "bolt_cap: SH-103 atomic_deduct call surface unavailable "
            "org=%s err=%s — using legacy peek",
            organization_id, e,
        )
        return _legacy_check_for_images(organization_id, n_images, bolts_per_image)
    if allowed:
        log.debug(
            "bolt_cap: atomic_reserve_bolts allowed org=%s n=%s "
            "bolts=%s remaining=%s",
            organization_id, n_images, requested_bolts, remaining,
        )
    else:
        log.info(
            "bolt_cap: atomic_reserve_bolts denied org=%s n=%s "
            "bolts_needed=%s remaining=%s",
            organization_id, n_images, requested_bolts, remaining,
        )
    return allowed, remaining


async def atomic_release_bolts(
    organization_id: str,
    n_images: int,
) -> bool:
    """Refund bolts previously reserved by :func:`atomic_reserve_bolts` (SH-181).

    Called when image processing fails after a successful reserve so the
    Redis consumed counter stays aligned with ``imagetocell_usage_log``.
    Without this, DLQ retries of a failing attachment over-consume Redis
    and falsely deny later work while the Hub UI still shows headroom.

    Returns ``True`` when the release ran (or was a no-op for ``n_images<=0``
    / no Redis). Returns ``False`` on Redis transport errors — callers must
    not fail the row for a refund miss; the counter can be reconciled later.
    """
    if n_images <= 0:
        return True

    from src.services.redis_client import get_redis_client

    redis_client = get_redis_client()
    if redis_client is None:
        log.debug(
            "bolt_cap: atomic_release_bolts skipped org=%s n=%s — no Redis",
            organization_id, n_images,
        )
        return True

    ctx = await load_i2c_org_usage_context_async(organization_id)
    bolts_per_image = (
        int(ctx.get("bolts_per_image") or DEFAULT_BOLTS_PER_IMAGE)
        if ctx else DEFAULT_BOLTS_PER_IMAGE
    )
    if bolts_per_image < 1:
        bolts_per_image = DEFAULT_BOLTS_PER_IMAGE
    amount = n_images * bolts_per_image
    consumed_key = _REDIS_CONSUMED_PREFIX.format(org_id=organization_id)

    try:
        new_consumed = await redis_client.atomic_release(consumed_key, amount)
    except Exception as e:
        log.warning(
            "bolt_cap: atomic_release_bolts failed org=%s n=%s bolts=%s err=%s",
            organization_id, n_images, amount, e,
        )
        return False

    log.info(
        "bolt_cap: atomic_release_bolts org=%s n=%s bolts=%s new_consumed=%s",
        organization_id, n_images, amount, new_consumed,
    )
    return True


def _ceiling_bolts_from_context(ctx: Optional[Dict[str, Any]]) -> Optional[int]:
    """Return the bolt ceiling for this org as a single integer, or None
    if the context is missing / does not have a meaningful cap.

    Combines the subscription ceiling with the hard-cap percentage exactly
    like the legacy ``get_remaining_image_capacity`` does.
    """
    if ctx is None:
        return None
    if (
        ctx.get("plan_suspended")
        or ctx.get("bolts_pending_payment")
        or ctx.get("hard_cap_reached_rpc")
    ):
        return 0  # Hard zero — the org cannot spend at all.
    ceiling = ctx.get("ceiling_bolts")
    if ceiling is None:
        return None
    return int(ceiling)


def _legacy_check_for_images(
    organization_id: str,
    n_images: int,
    bolts_per_image: int,
) -> Tuple[bool, int]:
    """Sync fallback used when no Redis client is wired.

    Wraps :func:`get_remaining_image_capacity` so the new async API still
    has a consistent ``(allowed, remaining_bolts)`` contract.
    """
    remaining_images, _ = get_remaining_image_capacity(organization_id)
    if remaining_images >= n_images:
        return True, max(0, (remaining_images - n_images) * bolts_per_image)
    return False, remaining_images * bolts_per_image


def invalidate_usage_cache(organization_id: str) -> None:
    """Drop the cached subscription / consumed-bolts snapshot for an org.

    Called at the end of a backfill so the next config in the same
    worksheet (or the next webhook event for the same org) sees the
    fresh consumed_bolts count instead of the 30-second-stale value.
    Without this, the cap context cache could let a second config
    skip past the budget check using a snapshot taken before the first
    config consumed its share.
    """
    with _usage_cache_lock:
        _usage_cache.pop(organization_id, None)
