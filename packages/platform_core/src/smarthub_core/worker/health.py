"""
Standardized health endpoint helper for workers.

Per `MIGRATION_PLAN.md` Phase 0 spec, every worker exposes `GET /health`
returning a uniform shape that the runtime supervisor (Railway, ECS health
check, future observability dashboard) can consume.

This module provides the **data**, not the HTTP framework. Each backend
mounts the report into its own FastAPI/Starlette app — that way
`smarthub_core` doesn't pull in a web framework as a hard dependency.

## Report shape

```json
{
    "status": "ok" | "degraded" | "down",
    "tool": "i2c",
    "stream": "stream:i2c",
    "group": "i2c",
    "queue_depth": 0,         // messages waiting in stream (lag)
    "pending": 0,             // delivered to group but not ACKed yet
    "last_delivered_at": "2026-04-30T15:00:00Z" | null,
    "redis": "ok" | "error: <msg>",
    "supabase": "ok" | "error: <msg>" | "not-configured"
}
```

## status semantics

- **down**: Redis is unreachable. The worker cannot consume; alarm operators.
- **degraded**: Redis ok, but Supabase failed health check OR `pending` is
  high (default >100, override via `pending_threshold`).
- **ok**: Redis up, Supabase up (or not configured), pending below threshold.

## FastAPI integration example

```python
from fastapi import FastAPI, Response
import json
from smarthub_core.worker.health import compute_health_report

app = FastAPI()

@app.get("/health")
async def health() -> Response:
    report = await compute_health_report(
        redis=rc,
        supabase=svc,
        tool="i2c",
        stream_name="stream:i2c",
        group_name="i2c",
    )
    code = 200 if report["status"] == "ok" else 503
    return Response(content=json.dumps(report), status_code=code, media_type="application/json")
```
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from redis.exceptions import RedisError

from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService


HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_DOWN = "down"

DEFAULT_PENDING_THRESHOLD = 100


async def compute_health_report(
    *,
    redis: RedisClient,
    tool: str,
    stream_name: str,
    group_name: str,
    supabase: Optional[SupabaseService] = None,
    pending_threshold: int = DEFAULT_PENDING_THRESHOLD,
) -> dict[str, Any]:
    """Build the health report dict for a worker.

    All checks are best-effort — failures are reported in the corresponding
    field, never raised. Callers map the report to an HTTP response.
    """
    report: dict[str, Any] = {
        "status": HEALTH_OK,
        "tool": tool,
        "stream": stream_name,
        "group": group_name,
        "queue_depth": 0,
        "pending": 0,
        "last_delivered_at": None,
        "redis": HEALTH_OK,
        "supabase": "not-configured",
    }

    redis_ok = await _check_redis(redis, stream_name, group_name, report)

    if supabase is not None:
        await _check_supabase(supabase, report)

    if not redis_ok:
        report["status"] = HEALTH_DOWN
    elif report["supabase"].startswith("error:") or report["pending"] > pending_threshold:
        report["status"] = HEALTH_DEGRADED

    return report


# ---------------------------------------------------------------------------
# Internal probes
# ---------------------------------------------------------------------------


async def _check_redis(
    redis: RedisClient,
    stream_name: str,
    group_name: str,
    report: dict[str, Any],
) -> bool:
    """Update `report` with Redis-derived fields. Returns True if Redis was reachable."""
    try:
        ping_ok = await redis.health_check()
    except Exception as e:
        report["redis"] = f"error: {type(e).__name__}: {e}"
        return False

    if not ping_ok:
        report["redis"] = "error: ping failed"
        return False

    # XINFO GROUPS gives both `lag` (messages not yet delivered to the group)
    # and `pending` (delivered but not ACKed). On unknown stream, RESP returns
    # an error — treat as zero rather than down.
    try:
        groups = await redis.raw.xinfo_groups(stream_name)
        for g in groups:
            if g.get("name") == group_name:
                report["queue_depth"] = int(g.get("lag") or 0)
                report["pending"] = int(g.get("pending") or 0)
                last_id = g.get("last-delivered-id")
                if last_id:
                    report["last_delivered_at"] = _stream_id_to_iso(last_id)
                break
        # If group is not found, leave defaults (0, 0, None). Worker may not
        # have started yet on a fresh deploy — that's not "down".
    except RedisError:
        # Stream might not exist yet — that's also OK. Defaults already set.
        pass
    except Exception as e:
        # Unexpected — surface but don't fail the whole probe
        report["redis"] = f"error: xinfo_groups: {type(e).__name__}: {e}"
        return False

    return True


async def _check_supabase(svc: SupabaseService, report: dict[str, Any]) -> None:
    """Run a trivial Supabase query to verify the connection. The query is
    `select 1 from a non-existent restful path` — we just want to confirm the
    HTTP layer responds; we accept either 200 OK or 4xx (PostgREST schema
    error). 5xx / connection failures mean Supabase is down.

    To keep this side-effect-free we run a lightweight `auth.get_user()`-style
    request via the underlying httpx client; an HTTP-level exception means
    network or DNS failure.
    """
    try:
        # Use the configured httpx client to issue a HEAD request to the
        # Supabase root. Cheaper than any postgrest call.
        response = await _supabase_head_check(svc)
        if response is not None and response < 500:
            report["supabase"] = HEALTH_OK
        else:
            report["supabase"] = f"error: status={response}"
    except Exception as e:
        report["supabase"] = f"error: {type(e).__name__}: {e}"


async def _supabase_head_check(svc: SupabaseService) -> Optional[int]:
    """Issue a HEAD against the Supabase URL using svc's httpx pool. Returns
    the HTTP status code or None on transport error."""
    import asyncio

    def _do_head() -> Optional[int]:
        try:
            # The supabase Client doesn't expose its base URL directly via a
            # stable property across versions. Pull from the postgrest client
            # which is the most stable path.
            base = str(svc.client.postgrest.base_url)
            r = svc.httpx_client.head(base, timeout=5.0)
            return r.status_code
        except Exception:
            raise

    return await asyncio.to_thread(_do_head)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream_id_to_iso(stream_id: str) -> Optional[str]:
    """Convert a Redis stream ID (`<ms>-<seq>`) to ISO 8601 UTC timestamp."""
    try:
        ms_str, _ = stream_id.split("-", 1)
        ts = datetime.fromtimestamp(int(ms_str) / 1000.0, tz=timezone.utc)
        return ts.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None
