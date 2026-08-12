#!/usr/bin/env python3
"""
SH-149 — dual-replica Redis dedup smoke (local).

No ngrok required. Smartsheet is NOT involved — we replay the same signed
webhook POST to two local I2C instances (or exercise dedup directly on Redis).

Usage (from backend/image-to-cell-3/, with venv + smarthub_core installed):

  # Level 1 — dedup only (Redis + shared SET NX). Fast, no .env beyond REDIS_URL.
  docker compose -f docker-compose.sh149.yml up redis -d
  ..\\venv\\Scripts\\python.exe scripts/sh149_dual_replica_smoke.py dedup

  # Level 2 — HTTP replay to two running replicas (needs .env + active I2C config).
  docker compose -f docker-compose.sh149.yml up --build -d
  ..\\venv\\Scripts\\python.exe scripts/sh149_dual_replica_smoke.py webhook \\
      --worksheet-id 1234567890123456

Environment:
  REDIS_URL          default redis://localhost:6379/0
  SH149_REPLICA_A    default http://localhost:3011
  SH149_REPLICA_B    default http://localhost:3012
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Allow imports from i2c-3 src when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

log = logging.getLogger("sh149_smoke")

_PROCESSING_PREFIX = "dedup:i2c:processing:"


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _replica_urls() -> Tuple[str, str]:
    a = os.environ.get("SH149_REPLICA_A", "http://localhost:3011").rstrip("/")
    b = os.environ.get("SH149_REPLICA_B", "http://localhost:3012").rstrip("/")
    return a, b


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_payload(
    *,
    worksheet_id: int,
    webhook_id: str,
    attachment_id: int,
) -> bytes:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "scopeObjectId": worksheet_id,
        "webhookId": webhook_id,
        "events": [
            {
                "eventType": "created",
                "objectType": "attachment",
                "id": attachment_id,
                "timestamp": now,
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _fetch_webhook_secret(worksheet_id: int) -> Tuple[str, str]:
    """Return (shared_secret, webhook_id) from Supabase for the worksheet."""
    from src.services.supabase.webhook_repo import WebhookRepository

    repo = WebhookRepository()
    reg = repo.get_by_worksheet(worksheet_id)
    if not reg or not reg.shared_secret:
        raise SystemExit(
            f"No webhook secret in DB for worksheet_id={worksheet_id}. "
            "Register a webhook for this sheet first."
        )
    return reg.shared_secret, str(reg.webhook_id)


async def run_dedup_smoke(*, burst: int = 20) -> None:
    """Prove SET NX dedup across two logical workers sharing one Redis."""
    from smarthub_core.db.redis import RedisClient
    from src.services.webhook_dedup import WebhookDedupService

    attachment_id = int(time.time() * 1000) % 9_000_000_000 + 1_000_000_000
    row_id = 0
    url = _redis_url()

    log.info("SH-149 dedup smoke: redis=%s attachment=%s row=%s", url, attachment_id, row_id)

    async with RedisClient(url) as client:
        svc_a = WebhookDedupService(client)
        svc_b = WebhookDedupService(client)

        if not svc_a.using_redis:
            raise SystemExit("Redis client not wired — check REDIS_URL and that Redis is up.")

        # Pairwise race: exactly one winner per concurrent pair.
        pair_results: List[bool] = await asyncio.gather(
            svc_a.try_acquire_processing(attachment_id, row_id),
            svc_b.try_acquire_processing(attachment_id, row_id),
        )
        wins = sum(1 for ok in pair_results if ok)
        if wins != 1:
            raise SystemExit(
                f"FAIL: expected exactly 1 winner in pairwise race, got {wins} ({pair_results})"
            )
        log.info("PASS: pairwise race — one winner, one loser (%s)", pair_results)

        # Burst across alternating services.
        attachment_burst = attachment_id + 1
        tasks = []
        for i in range(burst):
            svc = svc_a if i % 2 == 0 else svc_b
            tasks.append(svc.try_acquire_processing(attachment_burst, row_id))
        burst_results = await asyncio.gather(*tasks)
        burst_wins = sum(1 for ok in burst_results if ok)
        if burst_wins != 1:
            raise SystemExit(
                f"FAIL: expected 1 winner in burst of {burst}, got {burst_wins}"
            )
        log.info("PASS: burst of %d concurrent acquires — exactly 1 winner", burst)

        key = f"{_PROCESSING_PREFIX}{attachment_burst}:{row_id}"
        cached = await client.cache_get(key)
        if not cached:
            raise SystemExit(f"FAIL: expected Redis key {key} to exist after acquire")
        log.info("PASS: Redis key present — %s", key)

    print("\nSH-149 dedup smoke: ALL CHECKS PASSED")


async def _post_webhook(
    client: httpx.AsyncClient,
    base_url: str,
    body: bytes,
    signature: str,
    label: str,
) -> httpx.Response:
    log.info("POST %s/webhook/smartsheet (%s)", base_url, label)
    return await client.post(
        f"{base_url}/webhook/smartsheet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Smartsheet-Hmac-SHA256": signature,
        },
        timeout=30.0,
    )


async def run_webhook_smoke(*, worksheet_id: int) -> None:
    """Replay the same signed webhook to two local replicas in parallel."""
    from smarthub_core.db.redis import RedisClient

    secret, webhook_id = await _fetch_webhook_secret(worksheet_id)
    attachment_id = int(time.time() * 1000) % 9_000_000_000 + 2_000_000_000
    body = _build_payload(
        worksheet_id=worksheet_id,
        webhook_id=webhook_id,
        attachment_id=attachment_id,
    )
    signature = _sign(body, secret)
    replica_a, replica_b = _replica_urls()

    log.info(
        "SH-149 webhook replay: worksheet=%s attachment=%s replicas=%s + %s",
        worksheet_id,
        attachment_id,
        replica_a,
        replica_b,
    )

    async with httpx.AsyncClient() as http:
        resp_a, resp_b = await asyncio.gather(
            _post_webhook(http, replica_a, body, signature, "replica-a"),
            _post_webhook(http, replica_b, body, signature, "replica-b"),
        )

    for label, resp in (("replica-a", resp_a), ("replica-b", resp_b)):
        log.info("%s → HTTP %s", label, resp.status_code)
        if resp.status_code >= 500:
            raise SystemExit(f"FAIL: {label} returned {resp.status_code}: {resp.text[:500]}")

    async with RedisClient(_redis_url()) as redis:
        found_key = None
        for suffix in ("0", "None"):
            candidate = f"{_PROCESSING_PREFIX}{attachment_id}:{suffix}"
            if await redis.cache_get(candidate):
                found_key = candidate
                break
        if found_key:
            log.info("PASS: dedup processing key exists — %s", found_key)
        else:
            log.warning(
                "No dedup processing key for attachment=%s yet (check replica logs "
                "for [WEBHOOK:DEDUP] or no_active_configs)",
                attachment_id,
            )

    print(
        "\nSH-149 webhook replay: requests accepted on both replicas.\n"
        "Check docker logs for exactly one '[WEBHOOK:DEDUP] Skipping duplicate' "
        "and at most one processing path for the attachment."
    )
    print(f"  docker compose -f docker-compose.sh149.yml logs --tail=80 i2c-replica-a i2c-replica-b")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SH-149 dual-replica dedup smoke")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    dedup_p = sub.add_parser(
        "dedup",
        help="Redis-level cross-replica dedup (no I2C containers required)",
    )
    dedup_p.add_argument(
        "--burst",
        type=int,
        default=20,
        help="Concurrent acquire attempts in burst test (default 20)",
    )

    wh_p = sub.add_parser(
        "webhook",
        help="Parallel HTTP replay to two local I2C replicas (no ngrok)",
    )
    wh_p.add_argument(
        "--worksheet-id",
        type=int,
        required=True,
        help="Smartsheet worksheet_id with registered webhook + active I2C config",
    )

    args = parser.parse_args()
    _configure_logging(args.verbose)

    if args.command == "dedup":
        asyncio.run(run_dedup_smoke(burst=args.burst))
    elif args.command == "webhook":
        asyncio.run(run_webhook_smoke(worksheet_id=args.worksheet_id))


if __name__ == "__main__":
    main()
