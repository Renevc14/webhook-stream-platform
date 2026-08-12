"""SH-215 — SmartQR does not receive webhooks and owns no secret cache.

Smartsheet callbacks land on the shared gateway, which verifies the HMAC and
publishes to `stream:smart-qr`. SmartQR registers the webhook, stores the
encrypted secret in `smartqr_webhooks`, and consumes the stream. Anything that
reintroduces a receiver or a second writer of the gateway's cache is the bug
this file guards against.
"""

from __future__ import annotations

import inspect
from uuid import UUID

import pytest

from smart_qr.adapters.db import repositories as repo_mod
from smart_qr.adapters.db.repositories import WebhookRepository
from smart_qr.api.app import create_app
from smart_qr.config.settings import get_settings

ORG = UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _paths(app) -> set[str]:
    return set(app.openapi().get("paths", {}).keys())


# ----- no ingress ---------------------------------------------------------


def test_app_mounts_no_webhook_route():
    assert not [p for p in _paths(create_app()) if p.startswith("/webhook")]


def test_hub_api_and_health_still_mounted():
    """Removing ingress must not have taken the rest of the service with it."""
    paths = _paths(create_app())

    assert "/health" in paths
    assert "/api/v1/configs" in paths
    assert "/api/v1/configs/{config_id}/register-webhook" in paths


def test_webhook_receiver_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        __import__("smart_qr.api.webhooks")


# ----- no secret cache ----------------------------------------------------


def test_repository_exposes_no_secret_reader():
    """`secrets_map_for_org` existed only to verify HMACs locally."""
    assert not hasattr(WebhookRepository, "secrets_map_for_org")


def test_repository_holds_no_redis_helper():
    assert not hasattr(repo_mod, "_redis_client")


def test_upsert_does_not_take_plaintext():
    """Plaintext goes to the gateway push and nowhere else; keeping it here
    would invite a second cache writer."""
    params = inspect.signature(WebhookRepository.upsert).parameters

    assert "shared_secret_plaintext" not in params
    assert "shared_secret_encrypted" in params


def test_repository_source_never_names_the_gateway_cache():
    source = inspect.getsource(repo_mod)

    assert "webhook-secrets" not in source


# ----- m2m push -----------------------------------------------------------


@pytest.mark.asyncio
async def test_m2m_push_url_carries_the_provider(monkeypatch):
    from smart_qr.adapters.gateway import client as gw

    seen: dict[str, str] = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def put(self, url, headers=None, json=None):
            seen["url"] = url
            return FakeResponse()

    monkeypatch.setattr(gw.httpx, "AsyncClient", FakeClient)

    await gw.push_webhook_secrets(
        gateway_base_url="https://gw.example",
        m2m_token="t",
        tool_slug="smart-qr",
        organization_id=ORG,
        secrets={"901": "s"},
    )

    assert seen["url"] == (
        f"https://gw.example/internal/v1/webhook-secrets/smartsheet/smart-qr/{ORG}"
    )


@pytest.mark.asyncio
async def test_push_is_skipped_without_a_gateway_url(monkeypatch):
    """Local runs without a gateway must not fail registration."""
    from smart_qr.adapters.gateway import client as gw

    def _boom(*a, **kw):
        raise AssertionError("attempted an HTTP call with no gateway configured")

    monkeypatch.setattr(gw.httpx, "AsyncClient", _boom)

    await gw.push_webhook_secrets(
        gateway_base_url="",
        m2m_token="",
        tool_slug="smart-qr",
        organization_id=ORG,
        secrets={"901": "s"},
    )
