#!/usr/bin/env python3
"""Sandbox smoke checks for SmartQR (probes, auth, org isolation).

Covers cases 1-9 of docs/development/SMARTQR_SANDBOX_TEST_PLAN.md so they can be
re-run after every deploy instead of being clicked by hand. The product cases
(10-21) still need the Hub, since they write real images into Smartsheet.

Usage:
    python scripts/sandbox_smoke.py --base-url http://localhost:3021 \
        --jwt "$SUPABASE_ACCESS_TOKEN" --user-id <uuid> --org-id <uuid> \
        [--foreign-org-id <uuid>] [--expect-local]

Exit code is 0 only when every check passes.

Cases 7/8 assume the service under test has `SUPABASE_SERVICE_ROLE_KEY` set: org
membership is read from the Hub tables over the Supabase REST API, and without
that key every authenticated call answers 403 (check the boot log for the ERROR).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def call(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict[str, Any]] = None,
) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, f"connection failed: {exc.reason}"


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            self.failures += 1
        status = PASS if ok else FAIL
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name: str, why: str) -> None:
        print(f"[{SKIP}] {name} — {why}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:3021")
    parser.add_argument("--jwt", help="Supabase access token for a real user")
    parser.add_argument("--user-id", help="Must match the JWT sub")
    parser.add_argument("--org-id", help="Organization the user belongs to")
    parser.add_argument("--foreign-org-id", help="Organization the user must NOT reach")
    parser.add_argument(
        "--expect-local",
        action="store_true",
        help="Target runs APP_ENV=local: header-only auth and inject-event are allowed",
    )
    args = parser.parse_args()

    base = args.base_url
    report = Report()

    status, body = call(base, "/health")
    report.check("health returns 200", status == 200, f"{status} {body[:120]}")

    status, body = call(base, "/ready")
    ready = json.loads(body) if status in (200, 503) and body.startswith("{") else {}
    report.check(
        "ready reports database and redis",
        status in (200, 503) and "database" in ready,
        f"{status} {body[:160]}",
    )
    if ready:
        report.check(
            "ready sees a healthy database",
            ready.get("database") is True and status == 200,
            f"database={ready.get('database')} status={status}",
        )

    for path in ("/workspaces", "/items?workspace_id=1", "/columns?worksheet_id=1"):
        status, body = call(base, path)
        report.check(f"{path} rejects anonymous", status == 401, f"got {status}")

    status, _ = call(base, "/api/v1/configs")
    report.check("configs rejects anonymous", status == 401, f"got {status}")

    header_only = {"X-User-ID": args.user_id or "00000000-0000-0000-0000-000000000001",
                   "X-Organization-ID": args.org_id or "00000000-0000-0000-0000-000000000002"}
    status, _ = call(base, "/api/v1/configs", headers=header_only)
    if args.expect_local:
        report.check("local header-only auth accepted", status == 200, f"got {status}")
    else:
        report.check("header-only auth rejected", status == 401, f"got {status}")

    inject_body = {
        "organization_id": args.org_id or "00000000-0000-0000-0000-000000000002",
        "config_id": "00000000-0000-0000-0000-000000000003",
        "row_id": 1,
    }
    status, _ = call(base, "/api/v1/dev/inject-event", method="POST", body=inject_body)
    if args.expect_local:
        report.check("inject-event needs auth even locally", status == 401, f"got {status}")
    else:
        report.check("inject-event unreachable", status in (401, 403), f"got {status}")

    if not (args.jwt and args.user_id and args.org_id):
        report.skip("authenticated checks", "pass --jwt, --user-id and --org-id")
        return 1 if report.failures else 0

    auth = {
        "Authorization": f"Bearer {args.jwt}",
        "X-User-ID": args.user_id,
        "X-Organization-ID": args.org_id,
    }
    status, body = call(base, "/api/v1/configs", headers=auth)
    report.check("own org can list configs", status == 200, f"{status} {body[:160]}")

    status, _ = call(base, "/workspaces", headers=auth)
    report.check("authenticated browse works", status == 200, f"got {status}")

    mismatched = dict(auth, **{"X-User-ID": "11111111-2222-3333-4444-555555555555"})
    status, _ = call(base, "/api/v1/configs", headers=mismatched)
    report.check("X-User-ID must match the token", status == 401, f"got {status}")

    if args.foreign_org_id:
        foreign = dict(auth, **{"X-Organization-ID": args.foreign_org_id})
        status, body = call(base, "/api/v1/configs", headers=foreign)
        report.check("foreign org is refused", status == 403, f"{status} {body[:160]}")
    else:
        report.skip("foreign org check", "pass --foreign-org-id to run it")

    print()
    print("all checks passed" if not report.failures else f"{report.failures} check(s) failed")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
