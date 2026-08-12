"""SH-215 — provider-scoped secret cache keys.

Two properties: providers never share one secret map, and the pre-SH-215 key
(`webhook-secrets:{org}:{tool}`) has no reader or writer left anywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gateway.app import create_app
from gateway.secrets.store import (
    DEFAULT_PROVIDER,
    InMemoryWebhookSecretStore,
    secret_cache_key,
)

ORG = "org-abc"
TOOL = "smart-image-to-cell"
WEBHOOK_ID = "999888777"
SECRET = "test-shared-secret-1234567890"


# ----- key shape ----------------------------------------------------------


def test_key_carries_provider_segment():
    assert (
        secret_cache_key(ORG, TOOL)
        == f"webhook-secrets:{ORG}:{DEFAULT_PROVIDER}:{TOOL}"
    )


def test_provider_segment_is_normalized():
    """The URL segment is user-controlled casing; the key must not fork on it."""
    assert secret_cache_key(ORG, TOOL, "SmartSheet") == secret_cache_key(ORG, TOOL)


# ----- isolation ----------------------------------------------------------


@pytest.mark.asyncio
async def test_two_providers_on_one_tool_do_not_share_secrets():
    store = InMemoryWebhookSecretStore()
    await store.put_secrets(ORG, TOOL, {"1": "smartsheet-secret"})
    await store.put_secrets(ORG, TOOL, {"1": "slack-secret"}, provider="slack")

    assert await store.get_secrets(ORG, TOOL) == {"1": "smartsheet-secret"}
    assert await store.get_secrets(ORG, TOOL, provider="slack") == {
        "1": "slack-secret"
    }


@pytest.mark.asyncio
async def test_invalidating_one_provider_leaves_the_other():
    store = InMemoryWebhookSecretStore()
    await store.put_secrets(ORG, TOOL, {"1": "keep"})
    await store.put_secrets(ORG, TOOL, {"1": "drop"}, provider="slack")

    await store.invalidate(ORG, TOOL, provider="slack")

    assert await store.get_secrets(ORG, TOOL) == {"1": "keep"}
    assert await store.get_secrets(ORG, TOOL, provider="slack") == {}


# ----- the pre-SH-215 key is not a fallback -------------------------------

_PRE_SH215_KEY = f"webhook-secrets:{ORG}:{TOOL}"


@pytest.mark.asyncio
async def test_pre_sh215_key_is_never_read():
    """No compatibility read: the old key expires within one TTL and the
    lifecycle sync rebuilds the current one from Postgres on the same cycle.
    Reading it would keep a key alive that nothing is allowed to write."""
    store = InMemoryWebhookSecretStore({_PRE_SH215_KEY: {WEBHOOK_ID: SECRET}})

    assert await store.get_secrets(ORG, TOOL) == {}


@pytest.mark.asyncio
async def test_writes_never_touch_the_pre_sh215_key():
    store = InMemoryWebhookSecretStore()

    await store.put_secrets(ORG, TOOL, {WEBHOOK_ID: SECRET})

    assert _PRE_SH215_KEY not in store._secrets
    assert list(store._secrets) == [secret_cache_key(ORG, TOOL)]


# ----- ingress route ------------------------------------------------------


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, tool_slug: str, envelope: dict) -> str:
        self.published.append((tool_slug, envelope))
        return "1-0"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def client_and_store(request, monkeypatch):
    # Local m2m bypass requires no GATEWAY_M2M_TOKEN; clear cached Settings
    # so prior tests that set the token cannot force Bearer auth here.
    monkeypatch.delenv("GATEWAY_M2M_TOKEN", raising=False)
    monkeypatch.delenv("M2M_TOKEN", raising=False)
    from gateway.config.settings import get_settings

    get_settings.cache_clear()
    store = InMemoryWebhookSecretStore(getattr(request, "param", None))
    app = create_app(secret_store=store, publisher=_FakePublisher())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, store
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_and_store",
    [{secret_cache_key(ORG, TOOL): {WEBHOOK_ID: SECRET}}],
    indirect=True,
)
async def test_callback_verifies_against_provider_scoped_key(client_and_store):
    client, _ = client_and_store
    body = json.dumps({"webhookId": int(WEBHOOK_ID), "events": []}).encode()
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={"Smartsheet-Hmac-SHA256": _sign(body)},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_and_store",
    [{_PRE_SH215_KEY: {WEBHOOK_ID: SECRET}}],
    indirect=True,
)
async def test_callback_is_rejected_when_only_the_old_key_exists(client_and_store):
    """The old key is not an authority. A 401 here is correct and self-heals on
    the next sync cycle."""
    client, _ = client_and_store
    body = json.dumps({"webhookId": int(WEBHOOK_ID), "events": []}).encode()
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={"Smartsheet-Hmac-SHA256": _sign(body)},
    )
    assert resp.status_code == 401


# ----- internal m2m route -------------------------------------------------


@pytest.mark.asyncio
async def test_m2m_push_writes_provider_scoped_key(client_and_store):
    client, store = client_and_store
    resp = await client.put(
        f"/internal/v1/webhook-secrets/smartsheet/{TOOL}/{ORG}",
        json={"secrets": {WEBHOOK_ID: SECRET}},
    )
    assert resp.status_code == 200
    assert secret_cache_key(ORG, TOOL) in store._secrets


@pytest.mark.asyncio
async def test_m2m_push_has_no_two_segment_route(client_and_store):
    """One route only: an implicit provider is what SH-215 removed."""
    client, _ = client_and_store
    resp = await client.put(
        f"/internal/v1/webhook-secrets/{TOOL}/{ORG}",
        json={"secrets": {WEBHOOK_ID: SECRET}},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_m2m_push_routes_other_providers_separately(client_and_store):
    client, store = client_and_store
    await client.put(
        f"/internal/v1/webhook-secrets/slack/{TOOL}/{ORG}",
        json={"secrets": {WEBHOOK_ID: "slack-secret"}},
    )
    assert secret_cache_key(ORG, TOOL, "slack") in store._secrets
    assert secret_cache_key(ORG, TOOL) not in store._secrets
