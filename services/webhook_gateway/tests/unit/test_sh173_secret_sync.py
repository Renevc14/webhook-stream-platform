"""SH-173 — SecretCacheSync decrypt + group by org."""

from types import SimpleNamespace

import pytest
from smarthub_core.crypto import CryptoService

from gateway.lifecycle.secret_sync import SecretCacheSync
from gateway.secrets.store import InMemoryWebhookSecretStore


class FakeSupabase:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "imagetocell_webhooks"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)

    def execute_with_retry(self, operation, description="operation", **kwargs):
        return operation()


@pytest.fixture
def crypto():
    # 32 zero bytes, base64 — deterministic test key (NOT a production key).
    import base64
    return CryptoService(base64.b64encode(b"0" * 32).decode())


@pytest.mark.asyncio
async def test_refresh_all_decrypts_and_groups(crypto):
    enc_a = crypto.encrypt("secret-a")
    enc_b = crypto.encrypt("secret-b")
    rows = [
        {
            "organization_id": "org-1",
            "webhook_id": "111",
            "shared_secret": enc_a,
            "status": "active",
        },
        {
            "organization_id": "org-1",
            "webhook_id": "222",
            "shared_secret": enc_b,
            "status": "active",
        },
        {
            "organization_id": "org-2",
            "webhook_id": "333",
            "shared_secret": crypto.encrypt("secret-c"),
            "status": "active",
        },
    ]
    store = InMemoryWebhookSecretStore()
    sync = SecretCacheSync(
        supabase=FakeSupabase(rows),
        secret_store=store,
        crypto=crypto,
        tool_slug="smart-image-to-cell",
        secrets_ttl_s=60,
    )
    result = await sync.refresh_all()
    assert result.orgs_synced == 2
    assert result.secrets_cached == 3
    assert result.errors == 0
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {
        "111": "secret-a",
        "222": "secret-b",
    }
    assert await store.get_secrets("org-2", "smart-image-to-cell") == {
        "333": "secret-c",
    }


@pytest.mark.asyncio
async def test_refresh_org_invalidates_when_empty(crypto):
    store = InMemoryWebhookSecretStore()
    await store.put_secrets("org-x", "smart-image-to-cell", {"9": "old"})
    sync = SecretCacheSync(
        supabase=FakeSupabase([]),
        secret_store=store,
        crypto=crypto,
    )
    result = await sync.refresh_org("org-x")
    assert result.orgs_synced == 0
    assert await store.get_secrets("org-x", "smart-image-to-cell") == {}


@pytest.mark.asyncio
async def test_plaintext_legacy_secret_passes_through(crypto):
    store = InMemoryWebhookSecretStore()
    rows = [
        {
            "organization_id": "org-1",
            "webhook_id": "111",
            "shared_secret": "shortplain",
            "status": "active",
        }
    ]
    sync = SecretCacheSync(
        supabase=FakeSupabase(rows),
        secret_store=store,
        crypto=crypto,
    )
    result = await sync.refresh_all()
    assert result.secrets_cached == 1
    assert (await store.get_secrets("org-1", "smart-image-to-cell"))["111"] == "shortplain"
