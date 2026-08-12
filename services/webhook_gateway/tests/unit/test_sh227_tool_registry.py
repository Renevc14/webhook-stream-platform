"""SH-227 / SH-228 / SH-229 / SH-231 — tool registry control plane."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from gateway.app import create_app
from gateway.lifecycle.registrar import WebhookToolSpec, specs_for
from gateway.lifecycle.secret_sync import default_secret_sources
from gateway.routing.registry import HandlerRegistry
from gateway.secrets.store import InMemoryWebhookSecretStore
from gateway.streams.publisher import StreamPublisher, stream_for_tool
from gateway.tools.registry import (
    ToolRegistryEntry,
    UnknownToolError,
    load_tool_registry,
    parse_registry_json,
)


def test_builtin_registry_has_i2c_and_smartqr_not_ch2():
    reg = load_tool_registry()
    slugs = {e.tool_slug for e in reg.all_entries()}
    assert "smart-image-to-cell" in slugs
    assert "smart-qr" in slugs
    assert not any("cell-history" in s for s in slugs)


def test_i2c_registry_matches_imagetocell_webhooks_dialect():
    """I2C tool table has webhook_url and no runner_user_id (staging/prod)."""
    entry = load_tool_registry().require("smart-image-to-cell")
    assert entry.runner_column is None
    assert entry.callback_column == "webhook_url"
    spec = WebhookToolSpec.from_registry_entry(entry)
    assert spec.runner_column is None
    assert spec.callback_column == "webhook_url"


def test_i2c_legacy_short_slug_resolves_same_stream():
    reg = load_tool_registry()
    assert stream_for_tool("smart-image-to-cell", reg) == "stream:i2c"
    assert stream_for_tool("i2c", reg) == "stream:i2c"


def test_unknown_slug_fail_closed():
    reg = load_tool_registry()
    with pytest.raises(UnknownToolError):
        stream_for_tool("typo-tool", reg)


def test_json_overlay_adds_third_tool_without_python_constants():
    overlay = json.dumps(
        [
            {
                "tool_slug": "demo-tool",
                "stream_name": "demo-tool",
                "provider": "smartsheet",
                "configs_table": "demo_configs",
                "webhooks_table": "demo_webhooks",
                "secret_column": "shared_secret",
                "paused_column": "paused",
                "processing_mode_column": None,
                "secret_sync_enabled": True,
            }
        ]
    )
    reg = load_tool_registry(registry_json=overlay)
    entry = reg.require("demo-tool")
    assert entry.configs_table == "demo_configs"
    assert entry.stream_key() == "stream:demo-tool"
    sources = default_secret_sources(registry=reg)
    assert "demo-tool" in [s.tool_slug for s in sources]
    assert "smart-image-to-cell" in [s.tool_slug for s in sources]


def test_smartqr_secret_sync_can_disable_via_builtin_flag():
    reg = load_tool_registry(smartqr_secret_sync_enabled=False)
    sources = default_secret_sources(registry=reg)
    assert [s.tool_slug for s in sources] == ["smart-image-to-cell"]


def test_specs_for_allowlist_only():
    specs = specs_for(["smart-qr", "unknown-x"])
    assert [s.tool_slug for s in specs] == ["smart-qr"]


def test_ownership_prefix_uses_provider_not_smartsheet_literal():
    entry = ToolRegistryEntry(
        tool_slug="demo-tool",
        stream_name="demo-tool",
        provider="hubspot",
        configs_table="demo_configs",
        webhooks_table="demo_webhooks",
    )
    prefix = entry.ownership_prefix("https://webhooks.example.com")
    assert prefix == "https://webhooks.example.com/webhook/hubspot/demo-tool/"
    assert "smartsheet" not in prefix
    spec = WebhookToolSpec.from_registry_entry(entry)
    assert spec.provider == "hubspot"


def test_parse_registry_json_rejects_non_array():
    with pytest.raises(ValueError, match="JSON array"):
        parse_registry_json('{"tool_slug":"x"}')


@pytest.mark.asyncio
async def test_publisher_never_touches_redis_for_unknown_slug():
    redis = AsyncMock()
    redis.stream_publish = AsyncMock(return_value="1-0")
    pub = StreamPublisher(redis, registry=load_tool_registry())
    with pytest.raises(UnknownToolError):
        await pub.publish("no-such-tool", {"v": "1"})
    redis.stream_publish.assert_not_called()


@pytest.mark.asyncio
async def test_publisher_uses_registry_stream_name():
    redis = AsyncMock()
    redis.stream_publish = AsyncMock(return_value="9-0")
    pub = StreamPublisher(redis, registry=load_tool_registry())
    msg = await pub.publish("smart-image-to-cell", {"v": "1", "event_id": "e1"})
    assert msg == "9-0"
    assert redis.stream_publish.await_args.args[0] == "stream:i2c"


@pytest.mark.asyncio
async def test_ingress_returns_404_for_unknown_tool_after_hmac():
    import hashlib
    import hmac as hm

    from httpx import ASGITransport, AsyncClient

    secret = "test-shared-secret-1234567890"
    body = b'{"events":[{"objectType":"attachment","eventType":"created","id":1}]}'
    sig = hm.new(secret.encode(), body, hashlib.sha256).hexdigest()

    class _Pub:
        async def publish(self, tool_slug, envelope):
            raise UnknownToolError(tool_slug)

    store = InMemoryWebhookSecretStore()
    await store.put_secrets("org-1", "typo-tool", {"1": secret}, provider="smartsheet")
    app = create_app(
        registry=HandlerRegistry.default(),
        secret_store=store,
        publisher=_Pub(),  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/webhook/smartsheet/typo-tool/org-1",
            content=body,
            headers={"Smartsheet-Hmac-Sha256": sig},
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown tool"


def test_core_gateway_callback_url_accepts_provider():
    from smarthub_core.platforms.smartsheet.callback_urls import (
        gateway_callback_url,
        webhook_callback_ownership_patterns,
    )

    url = gateway_callback_url(
        "https://wh.example.com",
        "demo-tool",
        "org-1",
        provider="hubspot",
    )
    assert url == "https://wh.example.com/webhook/hubspot/demo-tool/org-1"
    patterns = webhook_callback_ownership_patterns(
        gateway_base_url="https://wh.example.com",
        legacy_callback_url=None,
        product_slug="demo-tool",
        provider="hubspot",
    )
    assert patterns == ["https://wh.example.com/webhook/hubspot/demo-tool/"]
