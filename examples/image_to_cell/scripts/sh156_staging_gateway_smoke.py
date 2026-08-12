"""SH-156 staging smoke — gateway HTTP path + signed publish (if secrets warm).

Does not print secrets. Pulls APP_CRYPTO_KEY from AWS Secrets Manager and
decrypts one imagetocell_webhooks.shared_secret via the Supabase REST API
using the same staging credentials already in the gateway secret.

Usage (from repo root, with AWS profile + network):
  set AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE as needed
  ./backend/image-to-cell-3/venv/Scripts/python.exe \\
    backend/image-to-cell-3/scripts/sh156_staging_gateway_smoke.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get(
    "GATEWAY_URL", "https://webhooks.prodactivelabs.com"
).rstrip("/")
I2C_READY = os.environ.get(
    "I2C_READY_URL", "https://smart-image-to-cell.prodactivelabs.com/ready"
)
TOOL = "smart-image-to-cell"
AWS_PROFILE = os.environ.get("AWS_PROFILE", "smarthub-staging")
AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
GW_SECRET_ID = "stg/webhook-gateway-env"
# Prefer PaidPlanTeam25 staging org (known active webhook).
ORG_ID = os.environ.get("SMOKE_ORG_ID", "2162bf72-6ae4-4124-81d4-18bc0d467c52")


def _aws_json(secret_id: str) -> dict:
    raw = subprocess.check_output(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_id,
            "--region",
            AWS_REGION,
            "--profile",
            AWS_PROFILE,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        text=True,
    )
    return json.loads(raw)


def _http(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 20,
) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return resp.status, dict(resp.headers.items()), data
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> int:
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "shared", "smarthub_core", "src"
        ),
    )
    from smarthub_core.crypto import CryptoService

    failures = 0
    print(f"Gateway: {GATEWAY}")
    print(f"I2C ready: {I2C_READY}")
    print(f"Org: {ORG_ID}\n")

    # --- 1 health ---
    print("=== 1. gateway /health ===")
    status, _, body = _http("GET", f"{GATEWAY}/health")
    ok = status == 200 and b'"ok"' in body
    failures += 0 if ok else 1
    print(f"  status={status} -> {'PASS' if ok else 'FAIL'}\n")

    # --- 2 challenge ---
    print("=== 2. challenge handshake ===")
    t0 = time.monotonic()
    status, hdrs, body = _http(
        "POST",
        f"{GATEWAY}/webhook/smartsheet/{TOOL}/{ORG_ID}",
        headers={"Smartsheet-Hook-Challenge": "sh156-tok"},
    )
    ms = int((time.monotonic() - t0) * 1000)
    # urllib lower-cases header names on some Python builds.
    hook_resp = next(
        (v for k, v in hdrs.items() if k.lower() == "smartsheet-hook-response"),
        None,
    )
    ok = status == 200 and body == b"sh156-tok" and hook_resp == "sh156-tok"
    failures += 0 if ok else 1
    print(f"  status={status} rtt={ms}ms -> {'PASS' if ok else 'FAIL'}\n")

    # --- 3 unknown org ---
    print("=== 3. unknown org -> 401 ===")
    status, _, _ = _http(
        "POST",
        f"{GATEWAY}/webhook/smartsheet/{TOOL}/unknown-org-sh156",
        headers={
            "Content-Type": "application/json",
            "Smartsheet-Hmac-SHA256": "00" * 32,
        },
        body=b"{}",
    )
    ok = status == 401
    failures += 0 if ok else 1
    print(f"  status={status} -> {'PASS' if ok else 'FAIL'}\n")

    # --- 4 i2c ready ---
    print("=== 4. i2c /ready (direct path still live) ===")
    status, _, body = _http("GET", I2C_READY)
    ok = status == 200 and b'"ready"' in body and b'"redis_lua"' in body
    failures += 0 if ok else 1
    print(f"  status={status} -> {'PASS' if ok else 'FAIL'}\n")

    # --- 5 signed callback (requires secret sync warm) ---
    print("=== 5. signed callback -> accepted (needs secret cache) ===")
    try:
        env = _aws_json(GW_SECRET_ID)
        crypto = CryptoService(env["APP_CRYPTO_KEY"])
        sb_url = env["SUPABASE_URL"].rstrip("/")
        key = env["SUPABASE_SERVICE_ROLE_KEY"]
        req = urllib.request.Request(
            f"{sb_url}/rest/v1/imagetocell_webhooks"
            f"?organization_id=eq.{ORG_ID}&status=eq.active"
            f"&select=webhook_id,shared_secret,worksheet_id&limit=1",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            rows = json.loads(resp.read().decode())
        if not rows:
            print("  SKIP: no active webhook row for org")
            failures += 1
        else:
            row = rows[0]
            plaintext = crypto.decrypt(row["shared_secret"])
            payload = {
                "webhookId": int(row["webhook_id"]),
                "scope": "sheet",
                "scopeObjectId": int(row["worksheet_id"]),
                "events": [
                    {
                        "objectType": "attachment",
                        "eventType": "created",
                        "id": 7594242852104068,
                    }
                ],
            }
            raw = json.dumps(payload).encode()
            t0 = time.monotonic()
            status, _, body = _http(
                "POST",
                f"{GATEWAY}/webhook/smartsheet/{TOOL}/{ORG_ID}",
                headers={
                    "Content-Type": "application/json",
                    "Smartsheet-Hmac-SHA256": _sign(plaintext, raw),
                },
                body=raw,
            )
            ms = int((time.monotonic() - t0) * 1000)
            accepted = status == 200 and b"accepted" in body
            print(f"  status={status} body={body.decode()[:120]} rtt={ms}ms")
            if accepted:
                print("  -> PASS (gateway accepted; consumer path needs CloudWatch)")
            else:
                print(
                    "  -> FAIL (if detail=no webhook secrets: secret sync not warm yet)"
                )
                failures += 1
    except Exception as exc:
        print(f"  -> FAIL setup: {type(exc).__name__}: {exc}")
        failures += 1

    print()
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
