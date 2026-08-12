"""
SmarthubWorker — abstract base class for stream consumers.

Implements the worker pattern from `TARGET_ARCHITECTURE.md` §Worker base class:

- Reads from `stream:{tool_slug}` via `XREADGROUP` with consumer group = `tool_slug`,
  consumer name = `f"{tool_slug}-{pod_uuid}"` (per architecture convention so
  `XPENDING` is attributable per pod and two pods cannot clobber each other's
  pending lists).
- Parses the versioned envelope `{v, event_id, tool, org_id, config_id,
  received_at, raw}`. Malformed envelopes go straight to DLQ + ack.
- Optional dedup gate via `dedup_key(job)` — subclasses override to enable.
  A non-`None` return value is `SET NX EX`'d before processing; if the key
  already exists, the message is acked and skipped (someone else has it).
- Calls subclass `process(job) -> Result` inside an exponential-backoff retry
  loop. `RetryableError` triggers a delay then retry. `FatalError` (or
  `RetryableError` after `max_attempts`) sends the message to
  `stream:{tool_slug}:dlq` with reason + traceback.
- **Ack ordering**: ACK happens AFTER `process()` succeeds and BEFORE publishing
  the optional `Result.platform_event` to `stream:platform-events`. This is
  the ordering specified in `TARGET_ARCHITECTURE.md` §Database update sequence
  Escenario 4. At-least-once delivery on the platform-events bus is accepted —
  consumers are required to be idempotent.
- DB writes happen inside `process()`. Postgres is the source of truth for
  what was processed; Redis is the source of truth for "currently in flight"
  (the dedup key).

## Example subclass

```python
class I2CWorker(SmarthubWorker):
    tool_slug = "i2c"

    def dedup_key(self, job: Job) -> str | None:
        # One attachment processed at most once across the cluster.
        att_id = job.raw.get("attachmentId")
        return f"dedup:i2c:{job.config_id}:{att_id}"

    async def process(self, job: Job) -> Result:
        # 1. Download + transform + upload (raises RetryableError on transient,
        #    FatalError on permanent issues like deleted sheet)
        # 2. UPDATE imagetocell_row_state ... (Postgres SoT)
        return Result(
            platform_event={
                "v": "1",
                "event_id": job.event_id,
                "type": "image_processed",
                "tool": "i2c",
                "org_id": job.org_id,
                "config_id": job.config_id,
            }
        )
```
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from smarthub_core.db.redis import RedisClient


log = logging.getLogger(__name__)

ENVELOPE_VERSION = "1"
PLATFORM_EVENTS_STREAM = "stream:platform-events"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkerError(Exception):
    """Base class for worker errors."""


class RetryableError(WorkerError):
    """Raised by `process()` when the failure is transient. The worker will
    sleep and retry up to `max_attempts` times. After exhaustion the message
    goes to DLQ."""


class FatalError(WorkerError):
    """Raised by `process()` when the failure is permanent. The message goes
    straight to DLQ with no retry."""


# ---------------------------------------------------------------------------
# Job / Result
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """A parsed event envelope from a stream message."""

    msg_id: str
    event_id: str
    tool: str
    org_id: str
    received_at: str
    raw: Any  # original Smartsheet payload — usually a dict (parsed) or string (JSON)
    config_id: Optional[str] = None
    payload: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    """Returned by `process()`. If `platform_event` is set, the envelope is
    published to `stream:platform-events` AFTER the consumer-group ACK."""

    success: bool = True
    platform_event: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class SmarthubWorker(ABC):
    """Abstract worker. Subclasses set `tool_slug` and implement `process()`.

    Optionally override `dedup_key(job)` to enable cross-instance dedup, and
    `dedup_ttl_s` to change the dedup window (default 600s).
    """

    # Subclasses MUST set this — it derives stream name + consumer group.
    tool_slug: str = ""

    # Retry config
    max_attempts: int = 3
    base_delay_s: float = 1.0

    # Dedup config — only used when `dedup_key(job)` returns non-None
    dedup_ttl_s: int = 600

    # Read loop config
    read_count: int = 10
    block_ms: int = 2000

    # Orphan recovery (opt-in, SH-212). A message whose consumer died between
    # XREADGROUP and ACK stays in the group's pending list forever. When
    # `claim_idle_ms` > 0 the worker periodically XAUTOCLAIMs entries idle
    # longer than that and re-handles them. Requires `process()` to be
    # idempotent — pair it with `dedup_key()` or content-level dedup.
    claim_idle_ms: int = 0
    claim_every_s: float = 15.0

    def __init__(
        self,
        redis: RedisClient,
        *,
        pod_id: Optional[str] = None,
        stream: Optional[str] = None,
        group: Optional[str] = None,
    ) -> None:
        if not self.tool_slug:
            raise WorkerError(
                f"{type(self).__name__} must set class attribute `tool_slug`"
            )

        self._redis = redis
        self._pod_id = pod_id or str(uuid.uuid4())
        self._stream = stream or f"stream:{self.tool_slug}"
        self._group = group or self.tool_slug
        self._consumer = f"{self.tool_slug}-{self._pod_id}"
        self._dlq_stream = f"{self._stream}:dlq"
        self._shutdown = asyncio.Event()

    # ----- public API -----------------------------------------------------

    @property
    def consumer_name(self) -> str:
        return self._consumer

    @property
    def stream_name(self) -> str:
        return self._stream

    @property
    def group_name(self) -> str:
        return self._group

    @property
    def dlq_stream_name(self) -> str:
        return self._dlq_stream

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown.is_set()

    @abstractmethod
    async def process(self, job: Job) -> Result:
        """Subclass implementation. Must be idempotent within the dedup window.

        Raise `RetryableError` for transient failures (will retry with backoff).
        Raise `FatalError` for permanent failures (will go to DLQ).
        Any other exception is treated as fatal (DLQ + ack).
        """

    def dedup_key(self, job: Job) -> Optional[str]:
        """Override to enable dedup. Return None to disable.

        Convention: `dedup:{tool}:{config}:{row}:{attachment}` — see
        TARGET_ARCHITECTURE.md §Caching strategy key #5.
        """
        return None

    async def run(self) -> None:
        """Main loop — block on XREADGROUP, dispatch each message.

        Returns when `request_shutdown()` is called. The shutdown signal is
        checked between every `xreadgroup` round, so an idle worker exits
        within at most `block_ms` ms of the request. Re-raises `RedisError`
        from the underlying stream consumer so the runtime supervisor (the
        process manager) can decide to reconnect or exit.
        """
        await self._redis.ensure_consumer_group(self._stream, self._group)

        log.info(
            "worker: starting tool=%s stream=%s group=%s consumer=%s",
            self.tool_slug,
            self._stream,
            self._group,
            self._consumer,
        )

        # We bypass `RedisClient.stream_consume` here because that generator
        # only yields on message receipt — leaving no opportunity to check
        # shutdown when the stream is idle. The direct xreadgroup loop checks
        # shutdown after every block.
        next_claim_at = 0.0
        while not self._shutdown.is_set():
            if self.claim_idle_ms > 0 and time.monotonic() >= next_claim_at:
                await self._reclaim_idle()
                next_claim_at = time.monotonic() + self.claim_every_s

            response = await self._redis.raw.xreadgroup(
                self._group,
                self._consumer,
                streams={self._stream: ">"},
                count=self.read_count,
                block=self.block_ms,
            )
            if not response:
                continue
            for _stream_name, messages in response:
                for msg_id, payload in messages:
                    if self._shutdown.is_set():
                        break
                    await self._handle_message(msg_id, payload)

        log.info(
            "worker: shutdown complete tool=%s consumer=%s",
            self.tool_slug,
            self._consumer,
        )

    def request_shutdown(self) -> None:
        """Signal the run loop to exit after the in-flight message completes."""
        self._shutdown.set()

    # ----- internal -------------------------------------------------------

    async def _reclaim_idle(self) -> None:
        """Take over pending entries abandoned by a dead consumer, then retry."""
        try:
            response = await self._redis.raw.xautoclaim(
                self._stream,
                self._group,
                self._consumer,
                min_idle_time=self.claim_idle_ms,
                start_id="0-0",
                count=self.read_count,
            )
        except Exception as e:
            # Older Redis builds have no XAUTOCLAIM. New messages still flow
            # through xreadgroup, so degrade quietly instead of crashing.
            log.warning(
                "worker: xautoclaim unavailable tool=%s: %s", self.tool_slug, e
            )
            return

        if not response:
            return
        # redis-py returns (next_id, entries, deleted_ids) — older builds omit
        # the third element.
        entries = response[1] if len(response) >= 2 else []
        if not entries:
            return

        log.info(
            "worker: reclaimed %d idle message(s) tool=%s consumer=%s",
            len(entries),
            self.tool_slug,
            self._consumer,
        )
        for msg_id, payload in entries:
            if self._shutdown.is_set():
                return
            if not payload:
                # Entry was trimmed/deleted — clear the dangling PEL record.
                await self._redis.stream_ack(self._stream, self._group, msg_id)
                continue
            await self._handle_message(msg_id, payload)

    async def _handle_message(self, msg_id: str, payload: dict[str, str]) -> None:
        # 1. Parse envelope. Malformed → DLQ + ack.
        try:
            job = self._parse_envelope(msg_id, payload)
        except WorkerError as e:
            log.error(
                "worker: malformed envelope tool=%s msg=%s: %s",
                self.tool_slug,
                msg_id,
                e,
            )
            await self._send_to_dlq(
                msg_id,
                payload,
                reason=f"envelope_parse_failed: {e}",
                event_id=payload.get("event_id", ""),
            )
            await self._redis.stream_ack(self._stream, self._group, msg_id)
            return

        # 2. Dedup gate (only if subclass returns a key).
        key = self.dedup_key(job)
        if key:
            claimed = await self._redis.dedup_set(key, ttl_s=self.dedup_ttl_s)
            if not claimed:
                log.info(
                    "worker: duplicate skipped tool=%s event=%s key=%s",
                    self.tool_slug,
                    job.event_id,
                    key,
                )
                await self._redis.stream_ack(self._stream, self._group, msg_id)
                return

        # 3. Process with retry. Fatal errors → DLQ + ack.
        try:
            result = await self._process_with_retry(job)
        except FatalError as e:
            log.error(
                "worker: fatal tool=%s event=%s: %s",
                self.tool_slug,
                job.event_id,
                e,
            )
            await self._send_to_dlq(
                msg_id,
                payload,
                reason=str(e),
                event_id=job.event_id,
                tb=traceback.format_exc(),
            )
            await self._redis.stream_ack(self._stream, self._group, msg_id)
            return
        except Exception as e:
            log.exception(
                "worker: unexpected error tool=%s event=%s",
                self.tool_slug,
                job.event_id,
            )
            await self._send_to_dlq(
                msg_id,
                payload,
                reason=f"unexpected: {type(e).__name__}: {e}",
                event_id=job.event_id,
                tb=traceback.format_exc(),
            )
            await self._redis.stream_ack(self._stream, self._group, msg_id)
            return

        # 4. ACK before platform-events publish (architecture rule).
        await self._redis.stream_ack(self._stream, self._group, msg_id)

        # 5. Best-effort publish to platform-events bus.
        if result is not None and result.platform_event is not None:
            await self._publish_platform_event(result.platform_event, job)

    async def _process_with_retry(self, job: Job) -> Optional[Result]:
        attempt = 0
        while True:
            try:
                return await self.process(job)
            except RetryableError as e:
                attempt += 1
                if attempt > self.max_attempts:
                    raise FatalError(
                        f"max_attempts ({self.max_attempts}) exhausted: {e}"
                    ) from e
                delay = self.base_delay_s * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                log.warning(
                    "worker: retry %d/%d tool=%s event=%s after %.2fs: %s",
                    attempt,
                    self.max_attempts,
                    self.tool_slug,
                    job.event_id,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

    def _parse_envelope(self, msg_id: str, payload: dict[str, str]) -> Job:
        v = payload.get("v")
        if v != ENVELOPE_VERSION:
            raise WorkerError(
                f"unsupported envelope version v={v!r}, expected {ENVELOPE_VERSION!r}"
            )

        event_id = payload.get("event_id")
        if not event_id:
            raise WorkerError("envelope missing event_id")

        # `raw` is stored as a string in the stream (Redis Streams only accept
        # strings). Try to JSON-decode for caller convenience; if not JSON,
        # pass through unchanged.
        raw_str = payload.get("raw", "")
        try:
            raw_decoded: Any = json.loads(raw_str) if raw_str else {}
        except (json.JSONDecodeError, TypeError):
            raw_decoded = raw_str

        return Job(
            msg_id=msg_id,
            event_id=event_id,
            tool=payload.get("tool", ""),
            org_id=payload.get("org_id", ""),
            config_id=payload.get("config_id") or None,
            received_at=payload.get("received_at", ""),
            raw=raw_decoded,
            payload=payload,
        )

    async def _send_to_dlq(
        self,
        msg_id: str,
        payload: dict[str, str],
        *,
        reason: str,
        event_id: str = "",
        tb: str = "",
    ) -> None:
        dlq_payload = {
            "v": ENVELOPE_VERSION,
            "tool": self.tool_slug,
            "msg_id": msg_id,
            "event_id": event_id,
            "reason": reason,
            "tb": tb,
            "original": json.dumps(payload),
        }
        result = await self._redis.stream_publish(self._dlq_stream, dlq_payload)
        if result is None:
            # DLQ publish failed — this is bad, but we still ack to prevent the
            # message looping forever in a degraded Redis state. Operator sees
            # the error log.
            log.error(
                "worker: DLQ publish failed tool=%s msg=%s reason=%s — message will be lost",
                self.tool_slug,
                msg_id,
                reason,
            )

    async def _publish_platform_event(
        self, event: dict[str, Any], job: Job
    ) -> None:
        """Publish a result event to the platform bus. Best-effort — failure is
        logged but does not block the worker (the message has already been ACKed)."""
        # Coerce values to strings (Redis Streams only accept strings).
        envelope: dict[str, str] = {
            "v": ENVELOPE_VERSION,
            "event_id": str(event.get("event_id") or job.event_id),
            "tool": str(event.get("tool") or self.tool_slug),
            "org_id": str(event.get("org_id") or job.org_id),
        }
        for k, v in event.items():
            if k in envelope:
                continue
            envelope[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)

        msg_id = await self._redis.stream_publish(PLATFORM_EVENTS_STREAM, envelope)
        if msg_id is None:
            log.warning(
                "worker: platform-events publish failed tool=%s event=%s — accepting loss",
                self.tool_slug,
                job.event_id,
            )
