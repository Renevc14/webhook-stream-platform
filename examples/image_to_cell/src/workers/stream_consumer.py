"""
Stream consumer lifecycle (SH-151 / SH-152).

SH-151 wired RUN_STREAM_CONSUMER into app lifespan with a placeholder task.
SH-152 replaces the placeholder with the real ``I2CStreamWorker`` — a
``SmarthubWorker`` subclass consuming ``stream:i2c`` (consumer group
``i2c-workers``) and delegating to the existing ``WebhookHandler`` pipeline.

Supervision: ``RedisError`` from the read loop (transport drop, failover)
triggers a bounded-backoff restart of ``run()`` instead of killing the task.
Cancellation (app shutdown / SIGTERM via lifespan) requests a graceful
worker shutdown so an in-flight message finishes before the task exits.

Degraded mode: when no Redis client is wired (no REDIS_URL, or Redis down at
startup), the task idles and logs — same policy as SH-99 dedup, the HTTP
webhook path keeps working directly.
"""

from __future__ import annotations

import asyncio
import logging

from redis.exceptions import RedisError

log = logging.getLogger(__name__)

_CONSUMER_TASK: asyncio.Task | None = None

# Backoff for supervisor restarts after RedisError (transport failures).
_RESTART_BASE_DELAY_S = 2.0
_RESTART_MAX_DELAY_S = 60.0


async def _run_supervised() -> None:
    """Run the I2C stream worker, restarting on transport failures."""
    from src.services.redis_client import get_redis_client

    redis_client = get_redis_client()
    if redis_client is None:
        log.warning(
            "SH-152 RUN_STREAM_CONSUMER=true but no Redis client wired — "
            "stream consumer idle (HTTP webhook path unaffected)"
        )
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.info("SH-152 idle stream consumer task cancelled")
            raise

    from src.workers.stream_worker import I2CStreamWorker

    delay = _RESTART_BASE_DELAY_S
    while True:
        worker = I2CStreamWorker(redis_client)
        log.info(
            "SH-152 stream consumer starting stream=%s group=%s consumer=%s",
            worker.stream_name,
            worker.group_name,
            worker.consumer_name,
        )
        try:
            await worker.run()
            # run() returns only after request_shutdown() — normal exit.
            log.info("SH-152 stream consumer exited cleanly")
            return
        except asyncio.CancelledError:
            # Lifespan shutdown — let the in-flight message finish.
            worker.request_shutdown()
            log.info("SH-152 stream consumer task cancelled — shutting down")
            raise
        except RedisError as e:
            log.warning(
                "SH-152 stream consumer Redis transport error (%s) — "
                "restarting in %.1fs",
                e,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RESTART_MAX_DELAY_S)
        except Exception:
            # Unexpected crash — same bounded backoff as transport errors.
            log.exception(
                "SH-152 stream consumer crashed unexpectedly — "
                "restarting in %.1fs",
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RESTART_MAX_DELAY_S)


async def start_stream_consumer() -> asyncio.Task:
    """Start the background stream consumer task."""
    global _CONSUMER_TASK
    if _CONSUMER_TASK is not None and not _CONSUMER_TASK.done():
        return _CONSUMER_TASK
    _CONSUMER_TASK = asyncio.create_task(_run_supervised())
    return _CONSUMER_TASK


async def stop_stream_consumer() -> None:
    """Cancel the stream consumer task on shutdown."""
    global _CONSUMER_TASK
    if _CONSUMER_TASK is None:
        return
    _CONSUMER_TASK.cancel()
    try:
        await _CONSUMER_TASK
    except asyncio.CancelledError:
        pass
    finally:
        _CONSUMER_TASK = None
