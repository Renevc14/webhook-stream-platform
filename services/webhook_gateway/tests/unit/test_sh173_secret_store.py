"""SH-173 — secret store put / get / invalidate."""

import pytest

from gateway.secrets.store import InMemoryWebhookSecretStore, secret_cache_key


@pytest.mark.asyncio
async def test_put_get_invalidate_roundtrip():
    store = InMemoryWebhookSecretStore()
    await store.put_secrets("org-1", "smart-image-to-cell", {"wh-1": "sec-aaa"})
    got = await store.get_secrets("org-1", "smart-image-to-cell")
    assert got == {"wh-1": "sec-aaa"}

    await store.invalidate("org-1", "smart-image-to-cell")
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {}


@pytest.mark.asyncio
async def test_put_empty_invalidates():
    store = InMemoryWebhookSecretStore()
    key = secret_cache_key("org-1", "smart-image-to-cell")
    store._secrets[key] = {"wh-1": "sec"}
    await store.put_secrets("org-1", "smart-image-to-cell", {})
    assert await store.get_secrets("org-1", "smart-image-to-cell") == {}


@pytest.mark.asyncio
async def test_orgs_are_isolated():
    store = InMemoryWebhookSecretStore()
    await store.put_secrets("org-a", "smart-image-to-cell", {"1": "a"})
    await store.put_secrets("org-b", "smart-image-to-cell", {"1": "b"})
    assert (await store.get_secrets("org-a", "smart-image-to-cell"))["1"] == "a"
    assert (await store.get_secrets("org-b", "smart-image-to-cell"))["1"] == "b"
