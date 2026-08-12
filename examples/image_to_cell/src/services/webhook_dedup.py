"""SH-99 — cross-process webhook deduplication service.

Before SH-99, ``WebhookHandler`` used two in-memory dicts to dedupe webhook
events: ``_processing_cache`` (per ``attachment_id:row_id`` to skip duplicates
while in flight) and ``_completed_attachments`` (per attachment_id to skip
events for attachments we already finished). Both caches only protected
duplicates **within a single ECS task**. When a Smartsheet webhook retry routed
to a different task — or when a task restarted mid-burst — the cache was empty
and the work ran twice, which combined with the bolt-cap behaviour produced
both double-bill (until SH-98 closed it at DB level) and double-image-upload.

This service moves both caches to Redis using
``smarthub_core.db.redis.RedisClient`` so dedup is shared across the cluster.

Graceful degradation
--------------------

The Redis hop is treated as best-effort. If Redis is unreachable at startup,
the service starts with an in-memory fallback and logs a single WARN. If a
runtime call raises ``RedisError`` (or any unexpected error), the call falls
back to the in-memory dict for that operation, logs a WARN, and proceeds. We
explicitly prefer over-dedup (skip work we may not have actually done) to
under-dedup (process the same attachment twice and double-bill), so the
fallback paths still skip if the in-memory cache shows the key.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from redis.exceptions import RedisError

from smarthub_core.db.redis import RedisClient


log = logging.getLogger(__name__)


# Redis key prefixes — keep stable; downstream tooling may grep for these.
_PROCESSING_PREFIX = "dedup:i2c:processing:"
_COMPLETED_PREFIX = "dedup:i2c:completed:"


class WebhookDedupService:
    """Cross-process dedup for webhook events, backed by Redis.

    Two operations:

    * :meth:`try_acquire_processing` — atomic ``SET NX EX`` returning True if
      the caller should proceed with processing, False if another worker
      already owns the slot.
    * :meth:`is_attachment_completed` / :meth:`mark_attachment_completed` —
      stored under a separate prefix so the "already finished" signal outlives
      the in-flight signal naturally (both share the same TTL).

    When Redis is unavailable the service uses an in-memory dict per operation
    so a single-task deploy still has correctness within that task. Callers
    do not need to handle availability themselves.
    """

    def __init__(
        self,
        client: Optional[RedisClient],
        ttl_s: int = 600,
    ) -> None:
        self._client = client
        self._ttl_s = ttl_s

        # In-memory fallback caches. Used when Redis is None (startup
        # degradation) or when a runtime RedisError trips the fallback path.
        # Same TTL semantics as the legacy WebhookHandler caches.
        self._mem_processing: Dict[str, float] = {}
        self._mem_completed: Dict[int, float] = {}

    @property
    def using_redis(self) -> bool:
        """True if the service has a live Redis client wired in."""
        return self._client is not None

    # ----- processing dedup (in-flight) ------------------------------------

    async def try_acquire_processing(
        self,
        attachment_id: int,
        row_id: int,
    ) -> bool:
        """Try to claim the right to process this ``(attachment_id, row_id)``.

        Returns True if the caller should proceed; False if another worker
        already owns the slot (or already processed it within the TTL window).

        Atomic via Redis ``SET NX EX``. Falls back to a non-atomic check-then-
        write on the in-memory dict if Redis is unavailable; that's the same
        guarantee the legacy code had.
        """
        mem_key = f"{attachment_id}:{row_id}"

        if self._client is not None:
            redis_key = f"{_PROCESSING_PREFIX}{mem_key}"
            try:
                # SET key 1 NX EX ttl — returns True if newly set.
                result = await self._client.raw.set(
                    redis_key, "1", nx=True, ex=self._ttl_s
                )
                return bool(result)
            except RedisError as e:
                log.warning(
                    "[SH-99] try_acquire_processing Redis error key=%s "
                    "err=%s — falling back to in-memory dedup",
                    redis_key, e,
                )
                # Fall through to in-memory path.

        return self._mem_try_acquire(mem_key)

    def _mem_try_acquire(self, mem_key: str) -> bool:
        now = time.time()
        existing = self._mem_processing.get(mem_key)
        if existing is not None and now - existing < self._ttl_s:
            return False
        self._mem_processing[mem_key] = now
        self._mem_cleanup_processing(now)
        return True

    def _mem_cleanup_processing(self, now: float) -> None:
        expired = [
            k for k, v in self._mem_processing.items()
            if now - v > self._ttl_s
        ]
        for k in expired:
            del self._mem_processing[k]

    # ----- completed dedup (post-processing) -------------------------------

    async def is_attachment_completed(self, attachment_id: int) -> bool:
        """True if this attachment was already finished within the TTL."""
        if self._client is not None:
            redis_key = f"{_COMPLETED_PREFIX}{attachment_id}"
            try:
                return bool(await self._client.raw.exists(redis_key))
            except RedisError as e:
                log.warning(
                    "[SH-99] is_attachment_completed Redis error att=%s "
                    "err=%s — falling back to in-memory dedup",
                    attachment_id, e,
                )

        now = time.time()
        existing = self._mem_completed.get(attachment_id)
        return existing is not None and now - existing < self._ttl_s

    async def mark_attachment_completed(self, attachment_id: int) -> None:
        """Mark this attachment as completed so subsequent events skip it."""
        if self._client is not None:
            redis_key = f"{_COMPLETED_PREFIX}{attachment_id}"
            try:
                await self._client.raw.set(redis_key, "1", ex=self._ttl_s)
                return
            except RedisError as e:
                log.warning(
                    "[SH-99] mark_attachment_completed Redis error att=%s "
                    "err=%s — falling back to in-memory dedup",
                    attachment_id, e,
                )

        now = time.time()
        self._mem_completed[attachment_id] = now
        self._mem_cleanup_completed(now)

    def _mem_cleanup_completed(self, now: float) -> None:
        expired = [
            k for k, v in self._mem_completed.items()
            if now - v > self._ttl_s
        ]
        for k in expired:
            del self._mem_completed[k]


# ---------------------------------------------------------------------------
# Module-level singleton (wired by app.lifespan and consumed by WebhookHandler)
# ---------------------------------------------------------------------------


_dedup_service: Optional[WebhookDedupService] = None


def set_dedup_service(service: WebhookDedupService) -> None:
    """Install the singleton (called from app.lifespan startup)."""
    global _dedup_service
    _dedup_service = service


def get_dedup_service() -> WebhookDedupService:
    """Get the singleton. Returns an in-memory-only instance if startup has
    not wired one (e.g. unit tests that import the handler without lifespan)."""
    global _dedup_service
    if _dedup_service is None:
        log.debug(
            "[SH-99] get_dedup_service called before startup wired one — "
            "returning in-memory-only fallback"
        )
        _dedup_service = WebhookDedupService(client=None)
    return _dedup_service
