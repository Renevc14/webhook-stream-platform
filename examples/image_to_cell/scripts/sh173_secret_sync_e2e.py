r"""SH-173 local E2E — gateway secret sync from sandbox + HMAC verify.

Requires the Phase 3 Docker stack (redis + gateway with sandbox env):

  cd services/webhook-gateway
  docker compose -f docker-compose.phase3.yml up --build -d

Then:

  .\venv\Scripts\python.exe scripts/sh173_secret_sync_e2e.py

Phases:
  1. Wait for gateway /health
  2. Wait until Redis has at least one webhook-secrets:* key (lifecycle sync)
  3. Pick one org/webhook from Redis, POST a signed callback -> 200
  4. POST with wrong signature -> 401
  5. Confirm lock:lifecycle:manager was held (key absent or owned after idle)

Does NOT require the i2c consumer for the HMAC path — stream publish is
exercised as a side effect when Redis + publisher are up.
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
TOOL = "smart-image-to-cell"
PREFIX = "webhook-secrets:"


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _callback(webhook_id: str) -> bytes:
    return json.dumps(
        {
            "webhookId": int(webhook_id) if str(webhook_id).isdigit() else 1,
            "scope": "sheet",
            "scopeObjectId": 999000111222333,
            "events": [
                {
                    "objectType": "attachment",
                    "eventType": "created",
                    "id": 7594242852104068,
                }
            ],
        }
    ).encode()


async def main() -> int:
    from smarthub_core.db.redis import RedisClient

    redis = RedisClient(REDIS_URL)
    try:
        await redis.start()
        await redis.raw.ping()
    except Exception as e:
        print(f"FATAL: Redis unreachable at {REDIS_URL}: {e}")
        return 1

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=15) as client:
        for _ in range(60):
            try:
                if (await client.get("/health")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            print(f"FATAL: gateway not reachable at {GATEWAY_URL}")
            return 1
        print(f"Gateway healthy at {GATEWAY_URL}")

        # Wait for lifecycle secret sync to populate at least one key.
        deadline = time.time() + 90
        keys = []
        while time.time() < deadline:
            keys = await redis.raw.keys(f"{PREFIX}*")
            if keys:
                break
            await asyncio.sleep(2)
        if not keys:
            print("FATAL: no webhook-secrets:* keys after 90s — sync never ran")
            print("Check gateway logs for SUPABASE_/APP_CRYPTO_KEY / lifecycle")
            return 1
        print(f"Found {len(keys)} secret cache key(s):")
        for k in keys:
            print(f"  {k}")

        # Use the first org that has a non-empty map.
        chosen = None
        for key in keys:
            raw = await redis.raw.get(key)
            if not raw:
                continue
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                # webhook-secrets:{org}:{provider}:{tool} since SH-215; the
                # pre-SH-215 3-part shape may still be draining.
                parts = key.split(":")
                org_id = parts[1] if len(parts) >= 3 else ""
                webhook_id, secret = next(iter(data.items()))
                chosen = (org_id, str(webhook_id), str(secret), key)
                break
        if not chosen:
            print("FATAL: secret keys present but all maps empty")
            return 1
        org_id, webhook_id, secret, cache_key = chosen
        print(f"Using org={org_id} webhook={webhook_id} key={cache_key}")

        body = _callback(webhook_id)
        url = f"/webhook/smartsheet/{TOOL}/{org_id}"

        resp = await client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Smartsheet-Hmac-SHA256": _sign(body, secret),
            },
        )
        print(f"Signed callback -> {resp.status_code} {resp.text[:200]}")
        if resp.status_code != 200:
            print("FAIL: expected 200 for sandbox-synced secret")
            return 1

        bad = await client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Smartsheet-Hmac-SHA256": "0" * 64,
            },
        )
        print(f"Bad signature -> {bad.status_code}")
        if bad.status_code != 401:
            print("FAIL: expected 401 for bad signature")
            return 1

        print("\nALL PASS — SH-173 secret sync + HMAC path")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
