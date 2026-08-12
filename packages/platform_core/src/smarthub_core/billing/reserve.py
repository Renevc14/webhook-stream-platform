"""Thin async wrappers over ``RedisClient.atomic_*`` for org-wide bolt caps."""

from __future__ import annotations

import logging
from typing import Optional, Tuple, Union
from uuid import UUID

from smarthub_core.billing.keys import (
    HOLD_TTL_S,
    LIMIT_TTL_S,
    consumed_key,
    cycle_id_key,
    hold_key,
    limit_key,
)
from smarthub_core.db.redis import RedisClient, RedisHelperError

log = logging.getLogger(__name__)

OrgId = Union[str, UUID]


async def seed_limit(
    redis: RedisClient,
    org_id: OrgId,
    ceiling: Optional[int],
    *,
    ttl_s: int = LIMIT_TTL_S,
) -> None:
    """Write or clear the cached ceiling so ``atomic_deduct*`` sees a fresh limit.

    ``ceiling is None`` deletes the limit key (default-open / billable overage).
    """
    key = limit_key(org_id)
    if ceiling is None:
        await redis.raw.delete(key)
        return
    await redis.raw.set(key, str(int(ceiling)), ex=ttl_s)


async def reserve_bolts(
    redis: RedisClient,
    org_id: OrgId,
    amount: int,
    *,
    cycle_id: str,
    cycle_baseline: int,
) -> Tuple[bool, int]:
    """Atomic check-and-increment against ``bolt-cap:{org}:*``.

    Returns ``(allowed, headroom)``. ``headroom == -1`` means no ceiling known.
    Raises :class:`RedisHelperError` on transport failure so callers can fall
    back to a Postgres snapshot.
    """
    if amount <= 0:
        raise RedisHelperError(f"reserve_bolts requires amount > 0, got {amount}")
    if not cycle_id:
        # Without a cycle id we cannot safely reset; still deduct against the
        # current counter with the known baseline seeded if the key is missing.
        await redis.raw.set(consumed_key(org_id), str(max(0, cycle_baseline)), nx=True)
        return await redis.atomic_deduct(
            consumed_key(org_id),
            limit_key(org_id),
            amount,
        )
    return await redis.atomic_deduct_with_cycle(
        consumed_key(org_id),
        limit_key(org_id),
        cycle_id_key(org_id),
        amount,
        cycle_id,
        max(0, cycle_baseline),
    )


async def release_bolts(redis: RedisClient, org_id: OrgId, amount: int) -> int:
    """Refund ``amount`` bolts previously reserved. Floors the counter at 0."""
    if amount <= 0:
        return 0
    return await redis.atomic_release(consumed_key(org_id), amount)


async def record_hold(
    redis: RedisClient,
    org_id: OrgId,
    operation_id: str,
    amount: int,
    *,
    ttl_s: int = HOLD_TTL_S,
) -> bool:
    """Remember the exact bolts held for ``operation_id`` (metadata only).

    TTL expiry deletes this key but does **not** decrement the aggregate
    consumed counter. Callers must still ``release_bolts`` on failure.
    """
    if amount <= 0 or not operation_id:
        return False
    return await redis.cache_set(hold_key(org_id, operation_id), str(amount), ttl_s)


async def read_hold(
    redis: RedisClient, org_id: OrgId, operation_id: str
) -> Optional[int]:
    """Return the exact bolts stored for ``operation_id``, or None if missing."""
    if not operation_id:
        return None
    raw = await redis.cache_get(hold_key(org_id, operation_id))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            "Malformed bolt hold org=%s operation_id=%s value=%r",
            org_id,
            operation_id,
            raw,
        )
        return None


async def clear_hold(redis: RedisClient, org_id: OrgId, operation_id: str) -> None:
    if not operation_id:
        return
    await redis.cache_del(hold_key(org_id, operation_id))
