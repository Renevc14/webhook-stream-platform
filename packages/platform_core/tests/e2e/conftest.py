"""
Fixtures and gating for end-to-end tests.

E2E tests are opt-in. They have two layers:

Layer A — local Redis only (no external deps).
    Enabled when SMARTHUB_E2E_REDIS=1 (or SMARTHUB_E2E_SANDBOX=1).
    Uses DB 14 (separate from the unit-test DB 15) and FLUSHDBs before
    each test for isolation.

Layer B — sandbox Supabase + sandbox Smartsheet.
    Enabled when SMARTHUB_E2E_SANDBOX=1.
    Requires sandbox credentials in environment plus a pre-seeded test org
    with smartsheet_tokens + organization_ss_api_base rows. Tests are
    read-only against external services to avoid sandbox state mutation.

Run from `shared/smarthub_core/`:

    # Layer A only
    SMARTHUB_E2E_REDIS=1 ../.venv/Scripts/python.exe -m pytest tests/e2e/ -v -m e2e

    # Both layers
    SMARTHUB_E2E_SANDBOX=1 ../.venv/Scripts/python.exe -m pytest tests/e2e/ -v -m e2e

When neither env var is set, all e2e tests pytest.skip cleanly.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.settings import CoreSettings


REDIS_E2E_URL = os.environ.get("REDIS_E2E_URL", "redis://127.0.0.1:6379/14")


def _layer_a_enabled() -> bool:
    return os.environ.get("SMARTHUB_E2E_REDIS") == "1" or _layer_b_enabled()


def _layer_b_enabled() -> bool:
    return os.environ.get("SMARTHUB_E2E_SANDBOX") == "1"


@pytest_asyncio.fixture
async def e2e_rc():
    """RedisClient for e2e tests. Uses DB 14, FLUSHDB before/after."""
    if not _layer_a_enabled():
        pytest.skip("Layer A not enabled (set SMARTHUB_E2E_REDIS=1 or SMARTHUB_E2E_SANDBOX=1)")
        return  # pragma: no cover

    client = RedisClient(REDIS_E2E_URL, max_connections=20, socket_timeout_s=2.0)
    try:
        await client.start()
    except Exception as e:
        pytest.skip(f"Redis e2e URL unreachable {REDIS_E2E_URL}: {e}")
        return  # pragma: no cover

    try:
        await client.raw.flushdb()
        yield client
    finally:
        try:
            await client.raw.flushdb()
        except Exception:
            pass
        await client.close()


@pytest.fixture
def sandbox_env() -> dict[str, str]:
    """Read sandbox credentials from env. Skip if Layer B not enabled or any
    required value is missing."""
    if not _layer_b_enabled():
        pytest.skip("Layer B not enabled (set SMARTHUB_E2E_SANDBOX=1)")

    required = [
        "APP_CRYPTO_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "E2E_ORG_ID",
        "E2E_PRODUCT_SLUG",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        pytest.skip(f"Layer B env vars missing: {missing}")
    return {k: os.environ[k] for k in required}


@pytest.fixture
def sandbox_settings(sandbox_env) -> CoreSettings:
    return CoreSettings(
        app_crypto_key=sandbox_env["APP_CRYPTO_KEY"],
        supabase_url=sandbox_env["SUPABASE_URL"],
        supabase_service_role_key=sandbox_env["SUPABASE_SERVICE_ROLE_KEY"],
        redis_url=REDIS_E2E_URL,
        # Calibrated for read-only e2e — keep pool small so we don't waste
        # sandbox connections.
        supabase_max_connections=3,
        supabase_max_keepalive=2,
        supabase_max_concurrent_ops=2,
    )


@pytest.fixture
def sandbox_supabase(sandbox_settings):
    """Real SupabaseService against sandbox. Closed automatically."""
    svc = SupabaseService(
        sandbox_settings.supabase_url,
        sandbox_settings.supabase_service_role_key,
        settings=sandbox_settings,
    )
    yield svc
    svc.close()
