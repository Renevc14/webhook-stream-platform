"""
Redis client and helpers for smarthub_core.

Implements the patterns specified in `docs/architecture/TARGET_ARCHITECTURE.md`:

- **Cache (cache-aside)**: `cache_get` returns `None` on miss OR on `RedisError`,
  so callers can fall back to the source of truth without crashing. Postgres is
  the source of truth; Redis caches it with explicit `DEL` on write.
- **Dedup**: `dedup_set` wraps `SET NX EX`. Redis is the source of truth.
- **Locks**: `acquire_lock` / `release_lock` / `extend_lock` use a token so a
  process cannot accidentally release a lock acquired by another. Release and
  extend run as Lua scripts to be atomic against expiration races.
- **Streams**: `stream_publish` (XADD), `stream_consume` (XREADGROUP, async
  iterator), `stream_ack` (XACK), `stream_dlq_publish` (XADD to `{stream}:dlq`).
- **Rate limit**: `rate_limit_consume` uses a Lua sliding-window script — one
  round-trip for `ZREMRANGEBYSCORE + ZCARD + ZADD`.

All methods swallow `RedisError` and log a warning. Failure modes:

| Method                | Sentinel on RedisError              |
|-----------------------|-------------------------------------|
| `cache_get`           | `None`                              |
| `cache_set`           | `False`                             |
| `cache_del`           | `0`                                 |
| `dedup_set`           | `False` (caller treats as "I won't run, someone else did") |
| `acquire_lock`        | `False`                             |
| `release_lock`        | `False`                             |
| `extend_lock`         | `False`                             |
| `stream_publish`      | `None`                              |
| `stream_ack`          | `0`                                 |
| `rate_limit_consume`  | `(True, 0)` — fail-open: do not break callers when Redis is down |
| `atomic_deduct`       | raises ``RedisHelperError`` on transport error (cap deny → ``(False, remaining)``) |
| `atomic_deduct_with_cycle` | same as ``atomic_deduct`` |
| `atomic_release`      | raises ``RedisHelperError`` on transport error |

Lua scripts are loaded once per ``RedisClient`` instance. If Redis restarts or
failovers (ElastiCache), cached SHA1 digests go stale; :meth:`_eval_lua`
detects ``NOSCRIPT`` / ``No matching script``, reloads the script, and retries
once before surfacing the error.

`stream_consume` propagates `RedisError` since the caller is a long-running
worker and needs to be aware of disconnects to reconnect.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator, Mapping, Optional, Tuple

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ResponseError


log = logging.getLogger(__name__)


class RedisHelperError(Exception):
    """Raised for programming errors against the Redis helpers (not transport errors)."""


# ---------------------------------------------------------------------------
# Lua scripts
# ---------------------------------------------------------------------------

# Release lock only if the caller still owns it.
_LUA_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

# Extend lock TTL only if the caller still owns it.
_LUA_EXTEND_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Sliding window rate limit.
# KEYS[1] = sorted set key
# ARGV[1] = current timestamp (seconds, float)
# ARGV[2] = window size in seconds
# ARGV[3] = max calls allowed in window
# Returns: {allowed (0|1), current_count_after_decision}
#
# Sorted set members must be unique. Two consecutive calls on platforms with
# coarse time.time() resolution (Windows in particular) can produce the same
# float, which would silently overwrite the previous ZADD entry. We append an
# INCR-driven sequence to the member so each insertion is distinct.
_LUA_RATE_LIMIT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_calls = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < max_calls then
    local seq = redis.call('INCR', KEYS[1] .. ':seq')
    redis.call('EXPIRE', KEYS[1] .. ':seq', math.ceil(window * 2))
    redis.call('ZADD', KEYS[1], now, tostring(now) .. '-' .. tostring(seq))
    redis.call('EXPIRE', KEYS[1], math.ceil(window * 2))
    return {1, count + 1}
else
    return {0, count}
end
"""


# Atomic check-and-deduct for billing / quota counters. Both reads and the
# conditional INCRBY happen inside the same Lua execution, so concurrent
# callers see consistent state even under high contention. If `consumed +
# requested <= limit`, the consumed counter is incremented by `requested`
# and the caller is `allowed=1` (with the remaining headroom). Otherwise
# the consumed counter is untouched and the caller is `allowed=0` (with the
# remaining headroom as it stands).
#
# Default-open semantics: when `limit_key` is missing we treat the limit as
# `+inf` (allow). The token bucket / quota loader is expected to populate
# `limit_key` from the source of truth on a TTL; defaulting to allow keeps
# billing paths going through a transient Redis hot-restart instead of
# hard-failing on a missing limit. Hard-cap correctness still falls back
# on the source-of-truth check at deploy time.
_LUA_ATOMIC_DEDUCT = """
local consumed = tonumber(redis.call('GET', KEYS[1]) or '0')
local raw_limit = redis.call('GET', KEYS[2])
local limit
if raw_limit == false or raw_limit == nil then
    limit = math.huge
else
    limit = tonumber(raw_limit)
end
local requested = tonumber(ARGV[1])
if consumed + requested <= limit then
    local new = redis.call('INCRBY', KEYS[1], requested)
    if limit == math.huge then
        return {1, -1}
    end
    return {1, limit - new}
else
    if limit == math.huge then
        return {0, -1}
    end
    return {0, limit - consumed}
end
"""


# Cycle-aware variant of atomic_deduct.
#
# Before evaluating the deduct, the script reads ``cycle_id_key`` and compares
# it to the caller's ``expected_cycle_id``. If they differ (or the key is
# missing — first encounter), the consumed counter is atomically reset to
# ``cycle_baseline`` and ``cycle_id_key`` is set to ``expected_cycle_id``.
# Only then does the standard check-and-deduct run.
#
# Use case: billing / quota counters that persist across a billing cycle but
# must reset when the cycle rolls over. The reset + deduct happens inside a
# single Lua execution, so two requests arriving in the exact millisecond
# of the rollover cannot race — exactly one applies the reset, the other
# sees the post-reset state, both deduct against the new cycle's counter.
#
# Same default-open / fail-closed semantics as `_LUA_ATOMIC_DEDUCT`.
_LUA_ATOMIC_DEDUCT_WITH_CYCLE = """
local stored_cycle = redis.call('GET', KEYS[3])
local expected_cycle = ARGV[2]
local cycle_baseline = tonumber(ARGV[3])
if stored_cycle == false or stored_cycle == nil or stored_cycle ~= expected_cycle then
    redis.call('SET', KEYS[1], tostring(cycle_baseline))
    redis.call('SET', KEYS[3], expected_cycle)
end

local consumed = tonumber(redis.call('GET', KEYS[1]) or '0')
local raw_limit = redis.call('GET', KEYS[2])
local limit
if raw_limit == false or raw_limit == nil then
    limit = math.huge
else
    limit = tonumber(raw_limit)
end
local requested = tonumber(ARGV[1])
if consumed + requested <= limit then
    local new = redis.call('INCRBY', KEYS[1], requested)
    if limit == math.huge then
        return {1, -1}
    end
    return {1, limit - new}
else
    if limit == math.huge then
        return {0, -1}
    end
    return {0, limit - consumed}
end
"""


# Refund units previously reserved by atomic_deduct*. Floors at 0 so a
# double-refund (or refund after reconcile) cannot drive the counter
# negative. Returns the new consumed value.
_LUA_ATOMIC_RELEASE = """
local consumed = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
if amount <= 0 then
    return consumed
end
local new = consumed - amount
if new < 0 then
    new = 0
end
redis.call('SET', KEYS[1], tostring(new))
return new
"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RedisClient:
    """Async Redis client wrapper with connection pooling and helper methods.

    Lifecycle:
        rc = RedisClient(url)
        await rc.start()
        ...
        await rc.close()

    Or use as an async context manager:
        async with RedisClient(url) as rc:
            await rc.cache_set("k", "v", 60)
    """

    def __init__(
        self,
        url: str,
        *,
        max_connections: int = 50,
        socket_timeout_s: float = 5.0,
        socket_connect_timeout_s: float = 5.0,
    ) -> None:
        self._url = url
        self._max_connections = max_connections
        self._socket_timeout_s = socket_timeout_s
        self._socket_connect_timeout_s = socket_connect_timeout_s

        self._pool: Optional[aioredis.ConnectionPool] = None
        self._client: Optional[aioredis.Redis] = None

        # Cached SHA1 of loaded Lua scripts (set on first use).
        self._sha_release_lock: Optional[str] = None
        self._sha_extend_lock: Optional[str] = None
        self._sha_rate_limit: Optional[str] = None
        self._sha_atomic_deduct: Optional[str] = None
        self._sha_atomic_deduct_with_cycle: Optional[str] = None
        self._sha_atomic_release: Optional[str] = None

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Initialize the connection pool and verify connectivity with PING."""
        self._pool = aioredis.ConnectionPool.from_url(
            self._url,
            max_connections=self._max_connections,
            decode_responses=True,
            socket_timeout=self._socket_timeout_s,
            socket_connect_timeout=self._socket_connect_timeout_s,
            retry_on_timeout=True,
        )
        self._client = aioredis.Redis(connection_pool=self._pool)
        try:
            await self._client.ping()
            log.info("redis: connected url=%s", self._sanitize_url())
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close all connections gracefully."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
        log.info("redis: closed url=%s", self._sanitize_url())

    async def __aenter__(self) -> "RedisClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def raw(self) -> aioredis.Redis:
        """Underlying redis.asyncio.Redis client (for advanced use)."""
        if self._client is None:
            raise RedisHelperError("RedisClient not started — call start() first")
        return self._client

    async def health_check(self) -> bool:
        """Return True if Redis is responsive to PING."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except RedisError as e:
            log.warning("redis: health_check failed: %s", e)
            return False

    async def lua_scripts_health_check(self) -> bool:
        """Return True if Lua scripts can be loaded and executed via EVALSHA.

        Exercises the same reload path used after ElastiCache failover or a
        local Redis container restart (stale cached SHA1 in the process).
        """
        if self._client is None:
            return False
        try:
            await self._eval_lua(
                _LUA_RATE_LIMIT,
                "_sha_rate_limit",
                1,
                "__health:lua_canary__",
                str(time.time()),
                "60",
                "1000",
            )
            return True
        except RedisError as e:
            log.warning("redis: lua_scripts_health_check failed: %s", e)
            return False

    def _sanitize_url(self) -> str:
        """Strip credentials from the URL for logging."""
        if "@" in self._url:
            return self._url.split("@", 1)[1]
        return self._url

    # ----- cache (cache-aside, sentinel on error) -------------------------

    async def cache_get(self, key: str) -> Optional[str]:
        """Return cached value or `None` on miss/error.

        Callers must treat `None` as "fetch from source of truth."
        """
        try:
            return await self.raw.get(key)
        except RedisError as e:
            log.warning("redis: cache_get failed key=%s err=%s", key, e)
            return None

    async def cache_set(self, key: str, value: str, ttl_s: int) -> bool:
        """Store value with TTL. Returns True on success, False on RedisError."""
        if ttl_s <= 0:
            raise RedisHelperError(f"cache_set requires ttl_s > 0, got {ttl_s}")
        try:
            await self.raw.set(key, value, ex=ttl_s)
            return True
        except RedisError as e:
            log.warning("redis: cache_set failed key=%s err=%s", key, e)
            return False

    async def cache_del(self, *keys: str) -> int:
        """Delete keys. Returns count deleted, 0 on RedisError."""
        if not keys:
            return 0
        try:
            return int(await self.raw.delete(*keys))
        except RedisError as e:
            log.warning("redis: cache_del failed keys=%s err=%s", keys, e)
            return 0

    # ----- dedup (Redis is source of truth) -------------------------------

    async def dedup_set(self, key: str, ttl_s: int) -> bool:
        """`SET NX EX ttl_s`. Returns True if key was newly set (caller proceeds),
        False if it already existed (caller skips) or RedisError."""
        if ttl_s <= 0:
            raise RedisHelperError(f"dedup_set requires ttl_s > 0, got {ttl_s}")
        try:
            result = await self.raw.set(key, "1", nx=True, ex=ttl_s)
            return bool(result)
        except RedisError as e:
            log.warning("redis: dedup_set failed key=%s err=%s", key, e)
            return False

    # ----- locks (token-checked release/extend via Lua) -------------------

    async def acquire_lock(self, name: str, token: str, ttl_s: int) -> bool:
        """Acquire a lock with a unique token. Returns True if acquired."""
        if ttl_s <= 0:
            raise RedisHelperError(f"acquire_lock requires ttl_s > 0, got {ttl_s}")
        if not token:
            raise RedisHelperError("acquire_lock requires a non-empty token")
        try:
            result = await self.raw.set(name, token, nx=True, ex=ttl_s)
            return bool(result)
        except RedisError as e:
            log.warning("redis: acquire_lock failed name=%s err=%s", name, e)
            return False

    async def release_lock(self, name: str, token: str) -> bool:
        """Release the lock only if `token` matches. Returns True if released."""
        try:
            result = await self._eval_lua(
                _LUA_RELEASE_LOCK, "_sha_release_lock", 1, name, token
            )
            return int(result) == 1
        except RedisError as e:
            log.warning("redis: release_lock failed name=%s err=%s", name, e)
            return False

    async def extend_lock(self, name: str, token: str, ttl_s: int) -> bool:
        """Extend the lock TTL only if `token` matches. Returns True if extended.

        Used for heartbeat: call every < TTL/2 seconds while holding the lock.
        """
        if ttl_s <= 0:
            raise RedisHelperError(f"extend_lock requires ttl_s > 0, got {ttl_s}")
        try:
            result = await self._eval_lua(
                _LUA_EXTEND_LOCK, "_sha_extend_lock", 1, name, token, str(ttl_s)
            )
            return int(result) == 1
        except RedisError as e:
            log.warning("redis: extend_lock failed name=%s err=%s", name, e)
            return False

    # ----- streams ---------------------------------------------------------

    async def ensure_consumer_group(
        self,
        stream: str,
        group: str,
        *,
        start_id: str = "0",
        mkstream: bool = True,
    ) -> None:
        """Idempotent XGROUP CREATE. Swallows BUSYGROUP (already exists)."""
        try:
            await self.raw.xgroup_create(stream, group, id=start_id, mkstream=mkstream)
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                return
            raise

    async def stream_publish(
        self, stream: str, payload: Mapping[str, str]
    ) -> Optional[str]:
        """XADD payload to stream. Returns message ID or None on RedisError.

        Payload values are stringified — Redis Streams only accept string fields.
        Use a versioned envelope (see TARGET_ARCHITECTURE.md §Stream payload envelope).
        """
        if not payload:
            raise RedisHelperError("stream_publish requires a non-empty payload")
        try:
            stringified = {k: _to_str(v) for k, v in payload.items()}
            return await self.raw.xadd(stream, stringified)
        except RedisError as e:
            log.warning("redis: stream_publish failed stream=%s err=%s", stream, e)
            return None

    async def stream_consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 2000,
    ) -> AsyncIterator[Tuple[str, dict]]:
        """Async iterator over (msg_id, payload) tuples from the stream.

        Re-raises `RedisError` so the worker can reconnect — long-running
        consumers must know about transport failures.

        The caller MUST `stream_ack` each message after successful processing.
        """
        while True:
            try:
                response = await self.raw.xreadgroup(
                    group,
                    consumer,
                    streams={stream: ">"},
                    count=count,
                    block=block_ms,
                )
            except RedisError:
                raise

            if not response:
                continue

            for _stream_name, messages in response:
                for msg_id, payload in messages:
                    yield msg_id, payload

    async def stream_ack(self, stream: str, group: str, *msg_ids: str) -> int:
        """XACK message(s). Returns count acked, 0 on RedisError."""
        if not msg_ids:
            return 0
        try:
            return int(await self.raw.xack(stream, group, *msg_ids))
        except RedisError as e:
            log.warning(
                "redis: stream_ack failed stream=%s ids=%s err=%s",
                stream,
                msg_ids,
                e,
            )
            return 0

    async def stream_dlq_publish(
        self, base_stream: str, payload: Mapping[str, str]
    ) -> Optional[str]:
        """Publish a message to the DLQ stream associated with `base_stream`.

        The DLQ stream is `{base_stream}:dlq` per architecture convention.
        Caller should include `event_id`, `reason`, `traceback`, and the
        original payload (or `event_id` to look it up later).
        """
        return await self.stream_publish(f"{base_stream}:dlq", payload)

    # ----- rate limit (Lua sliding window) --------------------------------

    async def rate_limit_consume(
        self,
        key: str,
        *,
        window_s: int,
        max_calls: int,
    ) -> Tuple[bool, int]:
        """Sliding-window rate limit. Atomically checks and consumes one slot.

        Returns:
            (allowed, current_count_in_window)

        On RedisError this fails open (returns `(True, 0)`) — preferable to
        breaking the caller when the rate-limiter itself is unavailable.
        """
        if window_s <= 0 or max_calls <= 0:
            raise RedisHelperError("rate_limit_consume requires window_s>0, max_calls>0")
        try:
            now = time.time()
            result = await self._eval_lua(
                _LUA_RATE_LIMIT,
                "_sha_rate_limit",
                1,
                key,
                str(now),
                str(window_s),
                str(max_calls),
            )
            allowed = int(result[0]) == 1
            count = int(result[1])
            return allowed, count
        except RedisError as e:
            log.warning("redis: rate_limit_consume failed key=%s err=%s — fail-open", key, e)
            return True, 0

    # ----- atomic deduct (Lua check-and-decrement for billing / quotas) ---

    async def atomic_deduct(
        self,
        consumed_key: str,
        limit_key: str,
        requested: int,
    ) -> Tuple[bool, int]:
        """Atomic check-and-deduct against a billing / quota counter.

        Reads ``limit_key`` and the current ``consumed_key`` counter, and if
        ``consumed + requested <= limit`` increments ``consumed_key`` by
        ``requested`` and returns ``(True, remaining)``. Otherwise leaves
        ``consumed_key`` untouched and returns ``(False, remaining)``. All
        of this happens inside a single Lua execution so two concurrent
        callers cannot read the same ``consumed`` and collectively overshoot
        the limit — closing the TOCTOU gap that plagued the per-process
        bolt cap in i2c-3.

        Default-open when ``limit_key`` is missing — treats the limit as
        ``+inf`` and returns ``(True, -1)`` to signal "limit unknown".
        Callers that need a hard ceiling should populate ``limit_key`` from
        the source of truth before invoking this; the default-open path
        keeps billing-critical flows going through a transient cache miss
        instead of hard-failing.

        On transport ``RedisError`` (including stale Lua SHA after a Redis
        restart when reload also fails) this raises :class:`RedisHelperError`
        so callers such as i2c-3 ``atomic_reserve_bolts`` can fall back to the
        Postgres peek path. A genuine cap denial still returns
        ``(False, remaining)`` with ``remaining >= 0``.

        Args:
            consumed_key: Redis key holding the consumed counter (integer).
                Persists per billing cycle; the caller is responsible for
                cycle rollover.
            limit_key: Redis key holding the cached limit (integer). May be
                short-lived (TTL'd cache from the source of truth).
            requested: How many units the caller intends to consume.

        Returns:
            ``(allowed, remaining)`` — ``remaining`` is ``-1`` when the
            limit is not known (default-open path).

        Raises:
            RedisHelperError: when ``requested <= 0``.
        """
        if requested <= 0:
            raise RedisHelperError(
                f"atomic_deduct requires requested > 0, got {requested}"
            )
        try:
            result = await self._eval_lua(
                _LUA_ATOMIC_DEDUCT,
                "_sha_atomic_deduct",
                2,
                consumed_key,
                limit_key,
                str(requested),
            )
            allowed = int(result[0]) == 1
            remaining = int(result[1])
            return allowed, remaining
        except RedisError as e:
            log.warning(
                "redis: atomic_deduct failed consumed=%s limit=%s err=%s "
                "— raising RedisHelperError for caller fallback",
                consumed_key, limit_key, e,
            )
            raise RedisHelperError(
                f"atomic_deduct transport error consumed={consumed_key} "
                f"limit={limit_key}: {e}"
            ) from e

    async def atomic_deduct_with_cycle(
        self,
        consumed_key: str,
        limit_key: str,
        cycle_id_key: str,
        requested: int,
        expected_cycle_id: str,
        cycle_baseline: int,
    ) -> Tuple[bool, int]:
        """Cycle-aware atomic check-and-deduct for billing / quota counters.

        Wraps :meth:`atomic_deduct` with automatic counter reset at billing
        cycle rollover. Before evaluating the deduct, the Lua script reads
        ``cycle_id_key`` and compares it to ``expected_cycle_id``. If they
        differ — or the key is missing on first encounter — the consumed
        counter is atomically reset to ``cycle_baseline`` and
        ``cycle_id_key`` is set to ``expected_cycle_id``. Only then does
        the standard check-and-deduct run, against the now-fresh counter.

        The reset + deduct happens inside a single Lua execution, so two
        requests arriving in the same millisecond as the rollover cannot
        race: exactly one resets, the other sees the post-reset state,
        both deduct against the new cycle's counter.

        Use case: production bolt-cap in i2c-3 (SH-135). The Redis
        ``bolt-cap:{org}:consumed`` counter persists across the cycle for
        atomicity, but Postgres (the source of truth) effectively resets
        ``consumed_bolts_start`` at the cycle boundary. Without this
        helper the Redis counter stays stale, blocking the org earlier
        than entitled in the new cycle.

        Same default-open (``limit_key`` missing → ``(True, -1)``) and
        transport-error (``RedisError`` → :class:`RedisHelperError`) contract
        as :meth:`atomic_deduct`.

        Args:
            consumed_key: Redis key holding the cycle's consumed counter.
            limit_key: Redis key holding the cached limit (may be TTL'd).
            cycle_id_key: Redis key holding the cycle identifier from the
                last reset. New rollovers are detected by string mismatch
                against ``expected_cycle_id``.
            requested: How many units the caller intends to consume.
            expected_cycle_id: The current cycle's identifier — typically
                derived from the source of truth (e.g. Postgres
                ``current_cycle_start`` ISO string).
            cycle_baseline: Initial value for ``consumed_key`` after a
                rollover. Pass the source-of-truth's known consumed total
                for the new cycle so any work already accounted for in
                Postgres is reflected.

        Returns:
            ``(allowed, remaining)``. ``remaining`` is ``-1`` when the
            limit is unknown (default-open).
        """
        if requested <= 0:
            raise RedisHelperError(
                f"atomic_deduct_with_cycle requires requested > 0, got {requested}"
            )
        if not expected_cycle_id:
            raise RedisHelperError(
                "atomic_deduct_with_cycle requires a non-empty expected_cycle_id"
            )
        if cycle_baseline < 0:
            raise RedisHelperError(
                f"atomic_deduct_with_cycle requires cycle_baseline >= 0, "
                f"got {cycle_baseline}"
            )
        try:
            result = await self._eval_lua(
                _LUA_ATOMIC_DEDUCT_WITH_CYCLE,
                "_sha_atomic_deduct_with_cycle",
                3,
                consumed_key,
                limit_key,
                cycle_id_key,
                str(requested),
                expected_cycle_id,
                str(cycle_baseline),
            )
            allowed = int(result[0]) == 1
            remaining = int(result[1])
            return allowed, remaining
        except RedisError as e:
            log.warning(
                "redis: atomic_deduct_with_cycle failed consumed=%s limit=%s "
                "cycle_id=%s err=%s — raising RedisHelperError for caller fallback",
                consumed_key, limit_key, cycle_id_key, e,
            )
            raise RedisHelperError(
                f"atomic_deduct_with_cycle transport error consumed={consumed_key} "
                f"limit={limit_key} cycle_id={cycle_id_key}: {e}"
            ) from e

    async def atomic_release(
        self,
        consumed_key: str,
        amount: int,
    ) -> int:
        """Refund ``amount`` units previously reserved via :meth:`atomic_deduct`.

        Decrements ``consumed_key`` by ``amount``, floored at 0. Used when a
        caller reserved budget (SH-103) but work failed before billing —
        without this the Redis counter drifts above Postgres/usage_log and
        falsely blocks later work (SH-181).

        Args:
            consumed_key: Redis key holding the cycle's consumed counter.
            amount: How many units to release (``> 0``).

        Returns:
            The new consumed counter value after the release.

        Raises:
            RedisHelperError: when ``amount <= 0`` or on transport error.
        """
        if amount <= 0:
            raise RedisHelperError(
                f"atomic_release requires amount > 0, got {amount}"
            )
        try:
            result = await self._eval_lua(
                _LUA_ATOMIC_RELEASE,
                "_sha_atomic_release",
                1,
                consumed_key,
                str(amount),
            )
            return int(result)
        except RedisError as e:
            log.warning(
                "redis: atomic_release failed consumed=%s amount=%s err=%s "
                "— raising RedisHelperError for caller fallback",
                consumed_key, amount, e,
            )
            raise RedisHelperError(
                f"atomic_release transport error consumed={consumed_key} "
                f"amount={amount}: {e}"
            ) from e

    # ----- internal -------------------------------------------------------

    async def _eval_lua(
        self,
        script: str,
        attr_name: str,
        numkeys: int,
        *args: str,
    ):
        """Run a cached Lua script, reloading once on stale SHA (NOSCRIPT)."""
        stale_retried = False
        while True:
            try:
                sha = await self._ensure_lua(script, attr_name)
                return await self.raw.evalsha(sha, numkeys, *args)
            except ResponseError as e:
                if not stale_retried and _is_stale_lua_script_error(e):
                    log.warning(
                        "redis: stale Lua SHA attr=%s err=%s — reloading script",
                        attr_name, e,
                    )
                    setattr(self, attr_name, None)
                    stale_retried = True
                    continue
                raise

    async def _ensure_lua(self, script: str, attr_name: str) -> str:
        """Load `script` once per RedisClient instance, cache its SHA1."""
        cached = getattr(self, attr_name)
        if cached is not None:
            return cached
        sha = await self.raw.script_load(script)
        setattr(self, attr_name, sha)
        return sha


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_stale_lua_script_error(exc: BaseException) -> bool:
    """True when Redis no longer has the SHA cached in this process."""
    if isinstance(exc, ResponseError):
        msg = str(exc)
        return "NOSCRIPT" in msg or "No matching script" in msg
    return False


def _to_str(v) -> str:
    """Coerce a stream-payload value to str — Redis Streams only accept strings."""
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.decode()
    return str(v)
