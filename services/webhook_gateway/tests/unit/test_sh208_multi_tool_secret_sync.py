"""SH-208 — multi-tool secret sync (I2C + SmartQR; no CH-2)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from smarthub_core.crypto import CryptoService

from gateway.lifecycle.secret_sync import (
    SecretCacheSync,
    default_secret_sources,
)
from gateway.secrets.store import InMemoryWebhookSecretStore


class MultiTableFakeSupabase:
    def __init__(self, tables: dict[str, list]):
        self._tables = tables
        self._current: str | None = None
        self._filters: dict = {}

    def table(self, name: str):
        self._current = name
        self._filters = {}
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def execute(self):
        assert self._current is not None
        rows = list(self._tables.get(self._current, []))
        for key, value in self._filters.items():
            rows = [r for r in rows if r.get(key) == value]
        return SimpleNamespace(data=rows)

    def execute_with_retry(self, operation, description="operation", **kwargs):
        return operation()


@pytest.fixture
def crypto():
    return CryptoService(base64.b64encode(b"0" * 32).decode())


def test_default_sources_include_smartqr_not_ch2():
    sources = default_secret_sources(smartqr_enabled=True)
    slugs = [s.tool_slug for s in sources]
    assert slugs == ["smart-image-to-cell", "smart-qr"]
    assert all("cell-history" not in s.table for s in sources)
    assert all("cell_history" not in s.table for s in sources)


def test_default_sources_can_disable_smartqr():
    sources = default_secret_sources(smartqr_enabled=False)
    assert [s.tool_slug for s in sources] == ["smart-image-to-cell"]


@pytest.mark.asyncio
async def test_refresh_all_writes_both_tool_keys(crypto):
    i2c_enc = crypto.encrypt("i2c-secret")
    qr_enc = crypto.encrypt("qr-secret")
    tables = {
        "imagetocell_webhooks": [
            {
                "organization_id": "org-1",
                "webhook_id": "111",
                "shared_secret": i2c_enc,
                "status": "active",
            }
        ],
        "smartqr_webhooks": [
            {
                "organization_id": "org-1",
                "webhook_id": "999",
                "shared_secret_encrypted": qr_enc,
                "status": "active",
            }
        ],
    }
    store = InMemoryWebhookSecretStore()
    sync = SecretCacheSync(
        supabase=MultiTableFakeSupabase(tables),
        secret_store=store,
        crypto=crypto,
        sources=default_secret_sources(),
        secrets_ttl_s=60,
    )
    result = await sync.refresh_all()
    assert result.errors == 0
    assert result.secrets_cached == 2
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {
        "111": "i2c-secret"
    }
    assert await store.get_secrets("org-1", "smart-qr") == {"999": "qr-secret"}


@pytest.mark.asyncio
async def test_bad_ciphertext_on_one_tool_does_not_block_other(crypto):
    tables = {
        "imagetocell_webhooks": [
            {
                "organization_id": "org-1",
                "webhook_id": "111",
                "shared_secret": crypto.encrypt("ok"),
                "status": "active",
            }
        ],
        "smartqr_webhooks": [
            {
                "organization_id": "org-1",
                "webhook_id": "999",
                "shared_secret_encrypted": "this-is-not-valid-aes-gcm-ciphertext!!!!",
                "status": "active",
            }
        ],
    }
    store = InMemoryWebhookSecretStore()
    sync = SecretCacheSync(
        supabase=MultiTableFakeSupabase(tables),
        secret_store=store,
        crypto=crypto,
        sources=default_secret_sources(),
    )
    result = await sync.refresh_all()
    assert result.errors >= 1
    assert (await store.get_secrets("org-1", "smart-image-to-cell"))["111"] == "ok"
    assert await store.get_secrets("org-1", "smart-qr") == {}


@pytest.mark.asyncio
async def test_refresh_org_empty_smartqr_does_not_wipe_i2c(crypto):
    store = InMemoryWebhookSecretStore()
    await store.put_secrets("org-1", "smart-image-to-cell", {"111": "keep"})
    await store.put_secrets("org-1", "smart-qr", {"999": "stale"})
    tables = {
        "imagetocell_webhooks": [
            {
                "organization_id": "org-1",
                "webhook_id": "111",
                "shared_secret": crypto.encrypt("keep"),
                "status": "active",
            }
        ],
        "smartqr_webhooks": [],
    }
    sync = SecretCacheSync(
        supabase=MultiTableFakeSupabase(tables),
        secret_store=store,
        crypto=crypto,
        sources=default_secret_sources(),
    )
    await sync.refresh_org("org-1")
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {"111": "keep"}
    assert await store.get_secrets("org-1", "smart-qr") == {}


@pytest.mark.asyncio
async def test_single_source_constructor_still_i2c_only(crypto):
    """SH-173 backward compatibility."""
    store = InMemoryWebhookSecretStore()
    rows = [
        {
            "organization_id": "org-1",
            "webhook_id": "1",
            "shared_secret": crypto.encrypt("s"),
            "status": "active",
        }
    ]
    # Reuse MultiTable with only I2C key; single-source still queries imagetocell.
    sync = SecretCacheSync(
        supabase=MultiTableFakeSupabase({"imagetocell_webhooks": rows}),
        secret_store=store,
        crypto=crypto,
        tool_slug="smart-image-to-cell",
    )
    result = await sync.refresh_all()
    assert result.secrets_cached == 1
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {"1": "s"}
