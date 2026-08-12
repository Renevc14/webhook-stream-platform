"""Database connection settings for managed Postgres (Supabase sandbox/staging)."""

from __future__ import annotations

from smart_qr.adapters.db.pool import build_pool_kwargs


def test_local_host_connects_without_tls():
    kwargs = build_pool_kwargs("postgresql://smartqr:smartqr@localhost:5433/smartqr")
    assert "ssl" not in kwargs
    assert "statement_cache_size" not in kwargs


def test_compose_service_host_counts_as_local():
    assert "ssl" not in build_pool_kwargs("postgresql://smartqr:smartqr@db:5432/smartqr")


def test_remote_host_requires_tls():
    kwargs = build_pool_kwargs("postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres")
    assert kwargs["ssl"] == "require"


def test_ssl_mode_can_be_forced_either_way():
    local = "postgresql://smartqr:smartqr@localhost:5433/smartqr"
    assert build_pool_kwargs(local, ssl_mode="require")["ssl"] == "require"
    remote = "postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres"
    assert "ssl" not in build_pool_kwargs(remote, ssl_mode="disable")


def test_pooler_disables_statement_cache():
    """pgBouncer in transaction mode cannot keep prepared statements."""
    kwargs = build_pool_kwargs(
        "postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"
    )
    assert kwargs["statement_cache_size"] == 0


def test_statement_cache_can_be_set_explicitly():
    kwargs = build_pool_kwargs(
        "postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres",
        statement_cache_size=50,
    )
    assert kwargs["statement_cache_size"] == 50
