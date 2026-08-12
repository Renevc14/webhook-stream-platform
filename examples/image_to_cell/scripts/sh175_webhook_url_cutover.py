"""SH-175 — cutover Smartsheet webhook callback URLs to the shared gateway.

Target URL shape:

  {BASE}/webhook/smartsheet/smart-image-to-cell/{organization_id}

Uses ``SmartsheetWebhookService.update_callback_url`` (retry + Change-Agent
path already on the client) and persists the new URL into
``imagetocell_webhooks`` (+ ``worksheet_webhooks`` when present).

Safety defaults to dry-run. Pass ``--apply`` to mutate.

Requires ``REDIS_URL`` reachable from the machine running ``--apply``
(SmartsheetClient boots Redis outside FastAPI lifespan). ElastiCache is
VPC-only — a local Docker Redis is fine when using staging Secrets Manager
creds for Supabase/crypto.

  python scripts/sh175_webhook_url_cutover.py
  python scripts/sh175_webhook_url_cutover.py --org-id <uuid> --apply \\
      --base-url https://webhooks.prodactivelabs.com
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "shared", "smarthub_core", "src"
    ),
)

TOOL_SLUG = "smart-image-to-cell"


def _gateway_url(base: str, org_id: str) -> str:
    return f"{base.rstrip('/')}/webhook/smartsheet/{TOOL_SLUG}/{org_id}"


def _load_rows(org_id: Optional[str]) -> List[Dict[str, Any]]:
    from src.services.supabase.client import get_supabase_service

    db = get_supabase_service()

    def _q():
        q = (
            db.table("imagetocell_webhooks")
            .select("webhook_id,worksheet_id,organization_id,webhook_url,status")
            .eq("status", "active")
        )
        if org_id:
            q = q.eq("organization_id", org_id)
        return q.execute()

    result = db.execute_with_retry(_q, "list webhooks for cutover")
    return list(result.data or [])


def _runner_for_org(org_id: str) -> Optional[str]:
    from src.services.supabase.client import get_supabase_service

    db = get_supabase_service()
    result = db.execute_with_retry(
        lambda: db.table("imagetocell_config")
        .select("runner_user_id,created_by")
        .eq("organization_id", org_id)
        .eq("paused", False)
        .limit(1)
        .execute(),
        f"pick runner for org {org_id}",
    )
    rows = result.data or []
    if not rows:
        return None
    return rows[0].get("runner_user_id") or rows[0].get("created_by")


def _persist_url(webhook_id: str, worksheet_id: int, new_url: str) -> None:
    from src.services.supabase.client import get_supabase_service

    db = get_supabase_service()
    db.execute_with_retry(
        lambda: db.table("imagetocell_webhooks")
        .update({"webhook_url": new_url})
        .eq("webhook_id", str(webhook_id))
        .execute(),
        f"persist cutover url webhook={webhook_id}",
    )
    try:
        db.execute_with_retry(
            lambda: db.table("worksheet_webhooks")
            .update({"webhook_url": new_url})
            .eq("worksheet_id", worksheet_id)
            .execute(),
            f"persist secondary cutover url sheet={worksheet_id}",
        )
    except Exception as exc:
        print(f"  WARN secondary table: {exc}")


async def _ensure_redis() -> None:
    """Install RedisClient so get_smartsheet_client works outside FastAPI lifespan."""
    from smarthub_core.db.redis import RedisClient
    from src.services.redis_client import get_redis_client, set_redis_client

    if get_redis_client() is not None:
        return
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "REDIS_URL is required for --apply (SmartsheetClient needs Redis)"
        )
    client = RedisClient(url)
    await client.start()
    set_redis_client(client)


async def main() -> int:
    parser = argparse.ArgumentParser(description="SH-175 gateway URL cutover")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate Smartsheet + DB (default is dry-run)",
    )
    parser.add_argument("--org-id", default=None)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL", ""),
        help="Gateway origin, e.g. https://webhooks.prodactivelabs.com",
    )
    args = parser.parse_args()
    apply = bool(args.apply)
    base = (args.base_url or "").strip()
    if not base:
        print("FATAL: set --base-url or SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL")
        return 1

    if apply:
        try:
            await _ensure_redis()
        except Exception as exc:
            print(f"FATAL: cannot init Redis: {exc}")
            return 1

    rows = _load_rows(args.org_id)
    print(f"Plan: {len(rows)} webhook(s) -> {base}  dry_run={not apply}")
    if not rows:
        return 0

    failures = 0
    for row in rows:
        org_id = str(row["organization_id"])
        wh_id = str(row["webhook_id"])
        sheet = int(row["worksheet_id"])
        old = row.get("webhook_url") or ""
        new = _gateway_url(base, org_id)
        print(f"- org={org_id} webhook={wh_id} sheet={sheet}")
        print(f"    from: {old}")
        print(f"    to:   {new}")
        if old.rstrip("/") == new.rstrip("/"):
            print("    skip: already on gateway URL")
            continue
        if not apply:
            continue
        try:
            from src.services.smartsheet.client import get_smartsheet_client
            from src.services.smartsheet.webhooks import SmartsheetWebhookService

            runner = _runner_for_org(org_id)
            svc = SmartsheetWebhookService(get_smartsheet_client(org_id, runner))
            ok = await svc.update_callback_url(int(wh_id), new)
            if not ok:
                raise RuntimeError("update_callback_url returned False")
            _persist_url(wh_id, sheet, new)
            print("    OK")
        except Exception as exc:
            failures += 1
            print(f"    FAIL: {exc}")

    if not apply:
        print("\nDry-run only. Re-run with --apply to mutate.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
