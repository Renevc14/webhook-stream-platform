#!/usr/bin/env python3
"""
Smoke tests against a running encrypt-tool (or BAU /encrypt).

Usage:
  set ENCRYPT_SMOKE_BASE_URL=http://localhost:3051
  set ENCRYPT_SMOKE_JWT=<Supabase access token>   # required if service enforces JWT
  optional: ENCRYPT_SMOKE_ORG_ID, ENCRYPT_SMOKE_PRODUCT_SLUG, ENCRYPT_SMOKE_PLAIN_TOKEN (fake)

Exit 0 if health OK and (optional) encrypt returns expected error without real Smartsheet token.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    import urllib.request
    import urllib.error

    base = (os.environ.get("ENCRYPT_SMOKE_BASE_URL") or "").rstrip("/")
    if not base:
        print("Set ENCRYPT_SMOKE_BASE_URL (e.g. http://localhost:3051)", file=sys.stderr)
        return 2

    # Health
    try:
        with urllib.request.urlopen(f"{base}/", timeout=10) as r:
            body = r.read().decode("utf-8", errors="replace")
        if r.status != 200:
            print(f"Health bad status {r.status}", file=sys.stderr)
            return 1
        if "ready" not in body.lower():
            print(f"Unexpected health body: {body[:200]}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        print(f"Health HTTP error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Health failed: {e}", file=sys.stderr)
        return 1

    print("OK: GET / health")

    jwt_tok = (os.environ.get("ENCRYPT_SMOKE_JWT") or "").strip()
    org = (os.environ.get("ENCRYPT_SMOKE_ORG_ID") or "00000000-0000-4000-8000-000000000001").strip()
    slug = (os.environ.get("ENCRYPT_SMOKE_PRODUCT_SLUG") or "smartsheet-api-test").strip()
    plain = (os.environ.get("ENCRYPT_SMOKE_PLAIN_TOKEN") or "invalid-token-for-smoke").strip()

    import json

    req_body = json.dumps(
        {"organization_id": org, "plain_token": plain, "product_slug": slug}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/encrypt",
        data=req_body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if jwt_tok:
        req.add_header("Authorization", f"Bearer {jwt_tok}")

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"Unexpected success encrypt status={r.status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"POST /encrypt → {e.code} (expected for bad token or bad org)")
        try:
            j = json.loads(body)
            if j.get("request_id"):
                print(f"  request_id: {j.get('request_id')}")
        except json.JSONDecodeError:
            print(f"  body: {body[:300]}")
        # 401 Smartsheet invalid, 403 not member, 404 org, 500 resolve — smoke only proves route reachable
        # 401: JWT missing/invalid or Smartsheet rejected token — proves route + auth stack alive
        if e.code in (401, 403, 404, 500):
            return 0
        return 1
    except Exception as e:
        print(f"POST /encrypt failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
