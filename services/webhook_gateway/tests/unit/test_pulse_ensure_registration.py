"""Gateway m2m ensure-registration (Pulse cutover kick)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes import internal
from gateway.secrets.store import InMemoryWebhookSecretStore


@pytest.fixture
def app_with_ensure(monkeypatch):
    store = InMemoryWebhookSecretStore()
    internal.configure_internal_routes(secret_store=store)

    manager = MagicMock()
    manager.ensure_registration = AsyncMock(
        return_value={
            "ok": True,
            "registered": 1,
            "tool_slug": "smart-image-to-cell",
            "org_id": "org-1",
        }
    )
    monkeypatch.setattr(
        "gateway.routes.internal.get_active_lifecycle_manager",
        lambda: manager,
    )
    monkeypatch.setenv("GATEWAY_M2M_TOKEN", "test-m2m")
    monkeypatch.setenv("ENVIRONMENT", "staging")

    from gateway.config.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(internal.router)
    yield app, manager
    get_settings.cache_clear()


def test_ensure_registration_ok(app_with_ensure):
    app, manager = app_with_ensure
    client = TestClient(app)
    resp = client.post(
        "/internal/v1/lifecycle/ensure-registration",
        headers={"Authorization": "Bearer test-m2m"},
        json={
            "tool_slug": "smart-image-to-cell",
            "org_id": "org-1",
            "worksheet_id": 1124052763627396,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    manager.ensure_registration.assert_awaited_once()
    kwargs = manager.ensure_registration.await_args.kwargs
    assert kwargs["tool_slug"] == "smart-image-to-cell"
    assert kwargs["org_id"] == "org-1"
    assert kwargs["worksheet_id"] == 1124052763627396


def test_ensure_registration_requires_bearer(app_with_ensure):
    app, _manager = app_with_ensure
    client = TestClient(app)
    resp = client.post(
        "/internal/v1/lifecycle/ensure-registration",
        json={"tool_slug": "smart-image-to-cell", "org_id": "org-1"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_manager_ensure_filters_sheet():
    from gateway.lifecycle.manager import LifecycleManager
    from gateway.lifecycle.registrar import RegistrarResult

    registrar = MagicMock()
    registrar._fetch_active_configs.return_value = [
        {
            "organization_id": "org-1",
            "worksheet_id": 111,
            "configuration_name": "a",
        },
        {
            "organization_id": "org-1",
            "worksheet_id": 222,
            "configuration_name": "b",
        },
        {
            "organization_id": "org-2",
            "worksheet_id": 111,
            "configuration_name": "c",
        },
    ]
    registrar.reconcile_org = AsyncMock(
        return_value=RegistrarResult(configs_checked=1, registered=1)
    )

    spec = MagicMock()
    spec.tool_slug = "smart-image-to-cell"

    manager = LifecycleManager(
        redis_client=MagicMock(),
        secret_store=MagicMock(),
        secret_sync=MagicMock(),
        registrar=registrar,
        registrar_specs=[spec],
    )

    out = await manager.ensure_registration(
        tool_slug="smart-image-to-cell",
        org_id="org-1",
        worksheet_id=111,
    )
    assert out["ok"] is True
    registrar.reconcile_org.assert_awaited_once()
    args = registrar.reconcile_org.await_args.args
    assert args[1] == "org-1"
    configs = args[2]
    assert len(configs) == 1
    assert int(configs[0]["worksheet_id"]) == 111
