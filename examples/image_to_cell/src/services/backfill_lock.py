"""SH-148 — distributed lock for concurrent backfill runs.

The public backfill API is worksheet-scoped (``POST /configs/backfill``),
so the lock key is ``lock:backfill:worksheet:{worksheet_id}``. This prevents
double-click / parallel BackgroundTask races from double-billing the same
rows while a backfill is already in progress.

When Redis is not wired (local tests, degraded startup), the lock is skipped
with a WARN — same graceful-degradation pattern as webhook dedup (SH-99).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from smarthub_core.db.redis import RedisClient

from src.services.redis_client import get_redis_client


log = logging.getLogger(__name__)

BACKFILL_ALREADY_RUNNING = (
    "backfill_already_running: another backfill is in progress for this config"
)

_LOCK_PREFIX = "lock:backfill:worksheet:"
_DEFAULT_TTL_S = 600
_DEFAULT_HEARTBEAT_S = 60


def worksheet_lock_key(worksheet_id: int) -> str:
    """Redis key for a worksheet-scoped backfill lock."""
    return f"{_LOCK_PREFIX}{worksheet_id}"


# ── SH-183: split acquire/hold so an HTTP route can secure the lease ──
# The config-scoped backfill endpoint must return 202 only after the
# concurrency lease is secured, then hand the SAME lease to the background
# worker (which heartbeats and finally releases it). Acquiring inside the
# worker instead would leave a window where two accepted requests both get
# 202 and race, or where the worker double-acquires and deadlocks.

@dataclass
class BackfillLease:
    """A held worksheet backfill lock, transferable to a background worker.

    ``client is None`` means Redis is not wired (local tests / degraded
    startup) and every operation is a no-op — same graceful degradation as
    ``hold_worksheet_backfill_lock``.
    """

    worksheet_id: int
    lock_name: str
    token: str
    ttl_s: int
    heartbeat_interval_s: int
    client: Optional[RedisClient]


async def acquire_worksheet_backfill_lease(
    worksheet_id: int,
    *,
    redis_client: Optional[RedisClient] = None,
    ttl_s: int = _DEFAULT_TTL_S,
    heartbeat_interval_s: int = _DEFAULT_HEARTBEAT_S,
) -> Optional[BackfillLease]:
    """Try to acquire the worksheet backfill lock without holding a context.

    Returns the lease when acquired (caller must ensure it is eventually
    released, normally by passing it to :func:`hold_backfill_lease`), or
    ``None`` when another backfill already holds the lock.
    """
    client = redis_client if redis_client is not None else get_redis_client()
    lock_name = worksheet_lock_key(worksheet_id)
    token = uuid.uuid4().hex

    if client is None:
        log.warning(
            "redis not wired: skipping backfill lease worksheet_id=%s",
            worksheet_id,
        )
        return BackfillLease(
            worksheet_id=worksheet_id,
            lock_name=lock_name,
            token=token,
            ttl_s=ttl_s,
            heartbeat_interval_s=heartbeat_interval_s,
            client=None,
        )

    acquired = await client.acquire_lock(lock_name, token, ttl_s)
    if not acquired:
        log.info(
            "backfill lease busy worksheet_id=%s lock=%s", worksheet_id, lock_name
        )
        return None

    log.info(
        "backfill lease acquired worksheet_id=%s lock=%s", worksheet_id, lock_name
    )
    return BackfillLease(
        worksheet_id=worksheet_id,
        lock_name=lock_name,
        token=token,
        ttl_s=ttl_s,
        heartbeat_interval_s=heartbeat_interval_s,
        client=client,
    )


async def release_backfill_lease(lease: BackfillLease) -> None:
    """Release a lease outside :func:`hold_backfill_lease` (e.g. when the
    route acquired it but a later synchronous check failed before handoff)."""
    if lease.client is None:
        return
    released = await lease.client.release_lock(lease.lock_name, lease.token)
    if released:
        log.info(
            "backfill lease released worksheet_id=%s lock=%s",
            lease.worksheet_id,
            lease.lock_name,
        )
    else:
        log.warning(
            "backfill lease release failed worksheet_id=%s lock=%s",
            lease.worksheet_id,
            lease.lock_name,
        )


@asynccontextmanager
async def hold_backfill_lease(lease: BackfillLease) -> AsyncIterator[None]:
    """Heartbeat-extend an already-acquired lease and release it on exit.

    Used by the background worker that received the lease from the HTTP
    route — it must NOT re-acquire the lock (that would deadlock against
    its own token).
    """
    if lease.client is None:
        yield
        return

    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease.heartbeat_interval_s)
            except asyncio.TimeoutError:
                extended = await lease.client.extend_lock(
                    lease.lock_name, lease.token, lease.ttl_s
                )
                if not extended:
                    log.warning(
                        "backfill lease heartbeat lost worksheet_id=%s lock=%s",
                        lease.worksheet_id,
                        lease.lock_name,
                    )
                    break

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        yield
    finally:
        stop.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await release_backfill_lease(lease)


@asynccontextmanager
async def hold_worksheet_backfill_lock(
    worksheet_id: int,
    *,
    redis_client: Optional[RedisClient] = None,
    ttl_s: int = _DEFAULT_TTL_S,
    heartbeat_interval_s: int = _DEFAULT_HEARTBEAT_S,
) -> AsyncIterator[bool]:
    """Acquire a worksheet backfill lock for the duration of the context.

    Yields ``True`` when the caller may proceed, ``False`` when another worker
    already holds the lock for this worksheet.
    """
    client = redis_client if redis_client is not None else get_redis_client()
    if client is None:
        log.warning(
            "redis not wired: skipping backfill lock worksheet_id=%s",
            worksheet_id,
        )
        yield True
        return

    lock_name = worksheet_lock_key(worksheet_id)
    token = uuid.uuid4().hex
    acquired = await client.acquire_lock(lock_name, token, ttl_s)
    if not acquired:
        log.info(
            "backfill lock busy worksheet_id=%s lock=%s",
            worksheet_id,
            lock_name,
        )
        yield False
        return

    log.info(
        "backfill lock acquired worksheet_id=%s lock=%s",
        worksheet_id,
        lock_name,
    )
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_interval_s)
            except asyncio.TimeoutError:
                extended = await client.extend_lock(lock_name, token, ttl_s)
                if not extended:
                    log.warning(
                        "backfill lock heartbeat lost worksheet_id=%s lock=%s",
                        worksheet_id,
                        lock_name,
                    )
                    break

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        yield True
    finally:
        stop.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        released = await client.release_lock(lock_name, token)
        if released:
            log.info(
                "backfill lock released worksheet_id=%s lock=%s",
                worksheet_id,
                lock_name,
            )
        else:
            log.warning(
                "backfill lock release failed worksheet_id=%s lock=%s",
                worksheet_id,
                lock_name,
            )
