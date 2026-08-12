from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import asyncpg

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

# Hosts that are always plain TCP without TLS (developer machine / compose network).
LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "postgres"}
# Advisory lock so concurrent tasks cannot apply the same migration twice.
MIGRATION_LOCK_KEY = 8_123_456_789


def build_pool_kwargs(
    database_url: str,
    *,
    ssl_mode: str = "auto",
    statement_cache_size: Optional[int] = None,
) -> dict[str, Any]:
    """Connection arguments for the target Postgres.

    Managed Postgres (Supabase sandbox/staging) needs TLS, and its transaction
    pooler cannot keep prepared statements, so the statement cache must be off
    when connecting through it.
    """
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    kwargs: dict[str, Any] = {}

    mode = (ssl_mode or "auto").strip().lower()
    if mode == "require":
        kwargs["ssl"] = "require"
    elif mode == "disable":
        pass
    elif host and host not in LOCAL_DB_HOSTS:
        kwargs["ssl"] = "require"

    if statement_cache_size is not None:
        kwargs["statement_cache_size"] = statement_cache_size
    elif "pooler." in host or port == 6543:
        # pgBouncer in transaction mode reuses server connections between queries.
        kwargs["statement_cache_size"] = 0

    return kwargs


async def init_pool(
    database_url: str,
    *,
    ssl_mode: str = "auto",
    statement_cache_size: Optional[int] = None,
) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        kwargs = build_pool_kwargs(
            database_url,
            ssl_mode=ssl_mode,
            statement_cache_size=statement_cache_size,
        )
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10, **kwargs)
        log.info(
            "Postgres pool initialized host=%s ssl=%s statement_cache=%s",
            urlparse(database_url).hostname,
            kwargs.get("ssl", "off"),
            kwargs.get("statement_cache_size", "default"),
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


def resolve_migrations_dir(migrations_dir: str | Path) -> Path:
    path = Path(migrations_dir)
    if path.is_absolute():
        return path
    # Prefer /app/migrations in container, else relative to backend/smart-qr
    for candidate in (
        Path("/app/migrations"),
        Path(__file__).resolve().parents[3] / "migrations",
        path,
    ):
        if candidate.exists():
            return candidate
    return path


async def apply_migrations(migrations_dir: str | Path) -> None:
    pool = get_pool()
    path = resolve_migrations_dir(migrations_dir)
    files = sorted(path.glob("*.sql"))
    async with pool.acquire() as conn:
        # Serialize across replicas: two tasks booting together must not race.
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS smartqr_schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            for f in files:
                applied = await conn.fetchval(
                    "SELECT 1 FROM smartqr_schema_migrations WHERE filename=$1", f.name
                )
                if applied:
                    continue
                sql = f.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO smartqr_schema_migrations(filename) VALUES ($1)",
                        f.name,
                    )
                log.info("Applied migration %s", f.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
