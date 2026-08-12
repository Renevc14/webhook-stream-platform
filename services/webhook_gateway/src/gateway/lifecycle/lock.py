"""Redis singleton lock for the gateway lifecycle manager (SH-173).

Key: ``lock:lifecycle:manager`` — TTL 600s, heartbeat every 60s.
Mirrors ``hold_worksheet_backfill_lock`` (SH-148) so only one gateway replica
runs a cycle at a time.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

LIFECYCLE_LOCK_KEY = "lock:lifecycle:manager"
DEFAULT_TTL_S = 600
DEFAULT_HEARTBEAT_S = 60


@asynccontextmanager
async def hold_lifecycle_lock(
    redis_client,
    *,
    lock_key: str = LIFECYCLE_LOCK_KEY,
    ttl_s: int = DEFAULT_TTL_S,
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_S,
    token: Optional[str] = None,
) -> AsyncIterator[bool]:
    """Acquire the lifecycle singleton lock for the duration of the context.

    Yields ``True`` when this replica may run a cycle, ``False`` when another
    holder already owns the lock.
    """
    if redis_client is None:
        log.warning("webhook_gateway: no Redis — skipping lifecycle lock")
        yield True
        return

    lock_token = token or uuid.uuid4().hex
    acquired = await redis_client.acquire_lock(lock_key, lock_token, ttl_s)
    if not acquired:
        log.info("webhook_gateway: lifecycle lock busy key=%s", lock_key)
        yield False
        return

    log.info("webhook_gateway: lifecycle lock acquired key=%s", lock_key)
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_interval_s)
            except asyncio.TimeoutError:
                extended = await redis_client.extend_lock(lock_key, lock_token, ttl_s)
                if not extended:
                    log.warning(
                        "webhook_gateway: lifecycle lock heartbeat lost key=%s",
                        lock_key,
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
        released = await redis_client.release_lock(lock_key, lock_token)
        if released:
            log.info("webhook_gateway: lifecycle lock released key=%s", lock_key)
        else:
            log.warning(
                "webhook_gateway: lifecycle lock release failed key=%s",
                lock_key,
            )
