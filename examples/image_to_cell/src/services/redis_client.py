"""SH-93 — shared accessor for the lifespan-managed ``RedisClient``.

A single ``RedisClient`` is constructed in :func:`src.api.app.lifespan` and
needs to be reachable from anywhere that wants to use the
``smarthub_core`` cluster-wide primitives (token cache, rate-limit, dedup,
atomic deduct). Rather than passing the client down through every
constructor we install it on module import via :func:`set_redis_client`
and let consumers fetch it via :func:`get_redis_client`.

``get_redis_client`` returns ``None`` when no client has been installed
(e.g. unit tests that import a consumer without spinning up the app).
Callers must handle that and degrade — ``SmartsheetClient``, the dedup
service, and the bolt-cap atomic helper all do.
"""

from __future__ import annotations

from typing import Optional

from smarthub_core.db.redis import RedisClient


_redis_client: Optional[RedisClient] = None


def set_redis_client(client: Optional[RedisClient]) -> None:
    """Install the lifespan-managed client (or ``None`` for the degraded path)."""
    global _redis_client
    _redis_client = client


def get_redis_client() -> Optional[RedisClient]:
    """Return the installed client, or ``None`` if startup did not wire one."""
    return _redis_client
