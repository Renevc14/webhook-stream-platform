"""SH-153 local E2E — signed webhook through gateway → stream:i2c → consumer.

Exercises the real Phase 3 path with the Docker stack from
``services/webhook-gateway/docker-compose.phase3.yml`` (redis + gateway +
i2c monolith with RUN_STREAM_CONSUMER=true):

  Phase 1: challenge handshake against the gateway (no body, <5s SLA path)
  Phase 2: signed callback → 200 accepted → one envelope on stream:i2c →
           consumed + acked by the i2c container (group i2c-workers)
  Phase 3: bad signature → 401, stream untouched
  Phase 4: unknown org (no cached secrets) → 401

Secrets: the gateway reads ``webhook-secrets:{org}:{provider}:{tool}`` from Redis
(cache-aside contract — in staging the I2C lifecycle manager will populate
it; here we seed it directly, which also validates the key format).

Usage:
  cd services/webhook-gateway
  docker compose -f docker-compose.phase3.yml up --build -d
  cd ../../backend/image-to-cell-3
  .\\venv\\Scripts\\python.exe scripts/sh153_gateway_e2e.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

import httpx

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "shared", "smarthub_core", "src"
    ),
)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:3020")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

ORG = "e2e-org-sh153"
TOOL = "smart-image-to-cell"
WEBHOOK_ID = "3285357287499652"
SECRET = "sh153-e2e-shared-secret"
STREAM = "stream:i2c"
GROUP = "i2c-workers"
PROVIDER = "smartsheet"
SECRET_KEY = f"webhook-secrets:{ORG}:{PROVIDER}:{TOOL}"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _callback_body() -> bytes:
    return json.dumps(
        {
            "webhookId": int(WEBHOOK_ID),
            "scope": "sheet",
            "scopeObjectId": 999000111222333,  # no config in sandbox — consumer acks with no_active_configs
            "events": [
                {
                    "objectType": "attachment",
                    "eventType": "created",
                    "id": 7594242852104068,
                }
            ],
        }
    ).encode()


async def _stream_stats(redis):
    length = await redis.raw.xlen(STREAM)
    try:
        groups = await redis.raw.xinfo_groups(STREAM)
    except Exception:
        groups = []
    g = next((g for g in groups if g["name"] == GROUP), None)
    pending = g["pending"] if g else None
    delivered = g["last-delivered-id"] if g else "0-0"
    return length, pending, delivered


async def main() -> int:
    from smarthub_core.db.redis import RedisClient

    redis = RedisClient(REDIS_URL)
    try:
        await redis.start()
        await redis.raw.ping()
    except Exception as e:
        print(f"FATAL: Redis unreachable at {REDIS_URL}: {e}")
        return 1

    # Seed the secret cache the way the lifecycle manager will in staging.
    await redis.raw.set(SECRET_KEY, json.dumps({WEBHOOK_ID: SECRET}))
    print(f"Seeded {SECRET_KEY}")

    failures = 0
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10) as client:
        # Wait for gateway to be up.
        for attempt in range(30):
            try:
                r = await client.get("/health")
                if r.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            print(f"FATAL: gateway not reachable at {GATEWAY_URL}")
            return 1
        print(f"Gateway healthy at {GATEWAY_URL}\n")

        url = f"/webhook/smartsheet/{TOOL}/{ORG}"

        # ---- Phase 1: challenge --------------------------------------
        print("=== Phase 1: Smartsheet challenge handshake ===")
        t0 = time.monotonic()
        r = await client.post(url, headers={"Smartsheet-Hook-Challenge": "e2e-tok"})
        dt_ms = (time.monotonic() - t0) * 1000
        ok = (
            r.status_code == 200
            and r.headers.get("Smartsheet-Hook-Response") == "e2e-tok"
            and r.content == b"e2e-tok"
        )
        failures += 0 if ok else 1
        print(f"  status={r.status_code} echo_ok={ok} rtt={dt_ms:.0f}ms "
              f"-> {'PASS' if ok else 'FAIL'}\n")

        # ---- Phase 2: signed callback consumed -----------------------
        print("=== Phase 2: signed callback -> stream -> consumer ack ===")
        len_before, _, delivered_before = await _stream_stats(redis)
        body = _callback_body()
        t0 = time.monotonic()
        r = await client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Smartsheet-Hmac-SHA256": _sign(body),
            },
        )
        dt_ms = (time.monotonic() - t0) * 1000
        accepted = r.status_code == 200 and r.json().get("status") == "accepted"
        print(f"  gateway: status={r.status_code} body={r.text} rtt={dt_ms:.0f}ms")

        # Wait for the i2c container's consumer to pick it up and ack.
        consumed = False
        for _ in range(40):
            await asyncio.sleep(0.5)
            length, pending, delivered = await _stream_stats(redis)
            if (
                length == len_before + 1
                and pending == 0
                and delivered > delivered_before
            ):
                consumed = True
                break
        ok = accepted and consumed
        failures += 0 if ok else 1
        print(f"  stream: xlen {len_before}->{length} pending={pending} "
              f"delivered {delivered_before}->{delivered}")
        print(f"  -> {'PASS' if ok else 'FAIL'}\n")

        # ---- Phase 3: bad signature ----------------------------------
        print("=== Phase 3: invalid signature rejected ===")
        len_before, _, _ = await _stream_stats(redis)
        r = await client.post(
            url,
            content=body,
            headers={"Smartsheet-Hmac-SHA256": "0" * 64},
        )
        length, _, _ = await _stream_stats(redis)
        ok = r.status_code == 401 and length == len_before
        failures += 0 if ok else 1
        print(f"  status={r.status_code} (want 401)  xlen delta="
              f"{length - len_before} (want 0)  -> {'PASS' if ok else 'FAIL'}\n")

        # ---- Phase 4: unknown org ------------------------------------
        print("=== Phase 4: org without cached secrets rejected ===")
        r = await client.post(
            f"/webhook/smartsheet/{TOOL}/unknown-org",
            content=body,
            headers={"Smartsheet-Hmac-SHA256": _sign(body)},
        )
        ok = r.status_code == 401
        failures += 0 if ok else 1
        print(f"  status={r.status_code} (want 401)  -> {'PASS' if ok else 'FAIL'}\n")

    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} PHASE(S) FAILED")
    await redis.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
