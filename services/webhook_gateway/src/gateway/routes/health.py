"""Health and readiness probes."""

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

_redis_ping: Optional[Callable[[], Any]] = None


def set_redis_ping(fn: Optional[Callable[[], Any]]) -> None:
    global _redis_ping
    _redis_ping = fn


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "webhook-gateway"}


@router.get("/ready")
async def ready() -> dict:
    redis_ok = True
    if _redis_ping is not None:
        try:
            await _redis_ping()
        except Exception as exc:
            log.warning("webhook_gateway: Redis readiness check failed: %s", exc)
            redis_ok = False
    return {
        "status": "ready" if redis_ok else "degraded",
        "redis": "ok" if redis_ok else "unavailable",
    }
