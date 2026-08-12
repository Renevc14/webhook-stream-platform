"""SH-152 local validation — I2CStreamWorker against Docker Redis.

Real Redis (no mocks on the transport): XADDs gateway-shaped envelopes to
``stream:i2c`` and runs the actual ``I2CStreamWorker.run()`` loop with only
the WebhookHandler mocked (no Smartsheet/Supabase side effects).

  Phase A: 3 valid envelopes → consumed, handler called 3x, all acked
           (XPENDING == 0), no DLQ entries
  Phase B: duplicate event_id → dedup skip (handler called once, both acked)
  Phase C: malformed raw → stream:i2c:dlq entry + acked
  Phase D: handler failure (success=False) → retry then DLQ after
           max_attempts

Usage:
  docker compose -f docker-compose.sh149.yml up redis -d
  .\\venv\\Scripts\\python.exe scripts/sh152_stream_consumer_validation.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "shared", "smarthub_core", "src"
    ),
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
STREAM = "stream:i2c"
DLQ = "stream:i2c:dlq"
GROUP = "i2c-workers"


def _envelope(raw: dict | str, event_id: str | None = None) -> dict:
    return {
        "v": "1",
        "event_id": event_id or str(uuid.uuid4()),
        "provider": "smartsheet",
        "tool": "i2c",
        "org_id": "org-sh152",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw if isinstance(raw, str) else json.dumps(raw),
    }


def _callback(sheet_id: int = 5830930674876292) -> dict:
    return {
        "webhookId": 3285357287499652,
        "scope": "sheet",
        "scopeObjectId": sheet_id,
        "events": [
            {
                "objectType": "attachment",
                "eventType": "created",
                "id": 7594242852104068,
            }
        ],
    }


async def _run_worker_until_idle(worker, redis, max_wait_s: float = 10.0) -> None:
    """Run the worker loop until the stream has no pending/new messages."""
    task = asyncio.create_task(worker.run())
    deadline = asyncio.get_event_loop().time() + max_wait_s
    try:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.3)
            info = await redis.raw.xpending(STREAM, GROUP)
            length = await redis.raw.xlen(STREAM)
            groups = await redis.raw.xinfo_groups(STREAM)
            delivered = next(
                (g["last-delivered-id"] for g in groups if g["name"] == GROUP),
                "0-0",
            )
            entries = await redis.raw.xrange(STREAM)
            last_id = entries[-1][0] if entries else "0-0"
            if info["pending"] == 0 and (length == 0 or delivered >= last_id):
                break
    finally:
        worker.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


async def main() -> int:
    from smarthub_core.db.redis import RedisClient
    from src.models.results import WebhookResult
    from src.workers.stream_worker import I2CStreamWorker

    redis = RedisClient(REDIS_URL)
    try:
        await redis.start()
        await redis.raw.ping()
    except Exception as e:
        print(f"FATAL: cannot reach Redis at {REDIS_URL}: {e}")
        return 1

    # Reset state from previous runs (streams + dedup keys).
    await redis.raw.delete(STREAM, DLQ)
    async for key in redis.raw.scan_iter("dedup:i2c:envelope:*"):
        await redis.raw.delete(key)
    print(f"Redis OK at {REDIS_URL} — streams reset\n")

    failures = 0

    def _handler(result=None):
        h = MagicMock(name="WebhookHandler")
        h.handle_webhook = AsyncMock(
            return_value=result
            or WebhookResult(success=True, status="ok", events_processed=1)
        )
        return h

    # ---------------- Phase A: 3 valid envelopes ----------------
    print("=== Phase A: 3 valid envelopes consumed + acked ===")
    for _ in range(3):
        await redis.stream_publish(STREAM, _envelope(_callback()))
    handler = _handler()
    worker = I2CStreamWorker(redis, webhook_handler=handler, pod_id="val-a")
    await _run_worker_until_idle(worker, redis)
    pending = (await redis.raw.xpending(STREAM, GROUP))["pending"]
    dlq_len = await redis.raw.xlen(DLQ)
    calls = handler.handle_webhook.await_count
    ok = calls == 3 and pending == 0 and dlq_len == 0
    failures += 0 if ok else 1
    print(f"  handler calls={calls} (want 3)  pending={pending} (want 0)  "
          f"dlq={dlq_len} (want 0)  -> {'PASS' if ok else 'FAIL'}\n")

    # ---------------- Phase B: duplicate event_id ----------------
    print("=== Phase B: duplicate event_id deduped ===")
    dup_id = str(uuid.uuid4())
    await redis.stream_publish(STREAM, _envelope(_callback(), event_id=dup_id))
    await redis.stream_publish(STREAM, _envelope(_callback(), event_id=dup_id))
    handler = _handler()
    worker = I2CStreamWorker(redis, webhook_handler=handler, pod_id="val-b")
    await _run_worker_until_idle(worker, redis)
    pending = (await redis.raw.xpending(STREAM, GROUP))["pending"]
    calls = handler.handle_webhook.await_count
    ok = calls == 1 and pending == 0
    failures += 0 if ok else 1
    print(f"  handler calls={calls} (want 1)  pending={pending} (want 0)  "
          f"-> {'PASS' if ok else 'FAIL'}\n")

    # ---------------- Phase C: malformed raw ----------------
    print("=== Phase C: malformed raw -> stream DLQ ===")
    before_dlq = await redis.raw.xlen(DLQ)
    await redis.stream_publish(STREAM, _envelope("not-json{"))
    handler = _handler()
    worker = I2CStreamWorker(redis, webhook_handler=handler, pod_id="val-c")
    await _run_worker_until_idle(worker, redis)
    after_dlq = await redis.raw.xlen(DLQ)
    pending = (await redis.raw.xpending(STREAM, GROUP))["pending"]
    ok = (after_dlq - before_dlq) == 1 and pending == 0 and handler.handle_webhook.await_count == 0
    failures += 0 if ok else 1
    print(f"  dlq delta={after_dlq - before_dlq} (want 1)  pending={pending} "
          f"(want 0)  handler calls={handler.handle_webhook.await_count} (want 0)  "
          f"-> {'PASS' if ok else 'FAIL'}")
    entries = await redis.raw.xrange(DLQ)
    if entries:
        mid, payload = entries[-1]
        print(f"  {DLQ} {mid}: reason={payload.get('reason', '')[:80]}")
    print()

    # ---------------- Phase D: handler failure -> retry -> DLQ ----------------
    print("=== Phase D: handler failure retries then DLQs ===")
    before_dlq = await redis.raw.xlen(DLQ)
    await redis.stream_publish(STREAM, _envelope(_callback()))
    handler = _handler(WebhookResult(success=False, status="error", message="boom"))
    worker = I2CStreamWorker(redis, webhook_handler=handler, pod_id="val-d")
    worker.max_attempts = 2
    worker.base_delay_s = 0.05
    await _run_worker_until_idle(worker, redis)
    after_dlq = await redis.raw.xlen(DLQ)
    pending = (await redis.raw.xpending(STREAM, GROUP))["pending"]
    calls = handler.handle_webhook.await_count
    ok = (after_dlq - before_dlq) == 1 and pending == 0 and calls == 3  # 1 + 2 retries
    failures += 0 if ok else 1
    print(f"  dlq delta={after_dlq - before_dlq} (want 1)  handler calls={calls} "
          f"(want 3: initial + 2 retries)  pending={pending} (want 0)  "
          f"-> {'PASS' if ok else 'FAIL'}\n")

    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} PHASE(S) FAILED")
    await redis.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
