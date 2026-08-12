"""Pulse cutover — I2C defers Smartsheet register to gateway ensure-registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_cutover_calls_gateway_ensure_not_smartsheet():
    from src.api.routes.configs import _register_webhook_for_config

    settings = MagicMock()
    settings.tool_owns_webhook_lifecycle = False
    settings.gateway_internal_url = "https://gw.example"
    settings.gateway_m2m_token = "m2m"
    settings.product_slug = "smart-image-to-cell"

    ensure = AsyncMock(return_value={"ok": True})

    with (
        patch("src.config.settings.get_settings", return_value=settings),
        patch(
            "src.adapters.gateway.client.ensure_webhook_registration",
            ensure,
        ),
        patch(
            "src.adapters.gateway.ensure_webhook_registration",
            ensure,
        ),
        patch(
            "src.services.smartsheet.webhooks.SmartsheetWebhookService"
        ) as svc_cls,
    ):
        ok = await _register_webhook_for_config(
            worksheet_id=111,
            org_id="org-1",
            config_name="fgh",
            owner_user_id="user-1",
        )

    assert ok is True
    ensure.assert_awaited_once()
    kwargs = ensure.await_args.kwargs
    assert kwargs["organization_id"] == "org-1"
    assert kwargs["worksheet_id"] == 111
    assert kwargs["tool_slug"] == "smart-image-to-cell"
    svc_cls.assert_not_called()


@pytest.mark.asyncio
async def test_cutover_without_gateway_env_returns_false():
    from src.api.routes.configs import _register_webhook_for_config

    settings = MagicMock()
    settings.tool_owns_webhook_lifecycle = False
    settings.gateway_internal_url = ""
    settings.gateway_m2m_token = ""
    settings.product_slug = "smart-image-to-cell"

    with patch("src.config.settings.get_settings", return_value=settings):
        ok = await _register_webhook_for_config(
            worksheet_id=111,
            org_id="org-1",
            config_name="fgh",
        )

    assert ok is False


@pytest.mark.asyncio
async def test_tool_owned_register_pushes_secret_to_gateway():
    from src.api.routes.configs import _register_webhook_for_config

    settings = MagicMock()
    settings.tool_owns_webhook_lifecycle = True
    settings.gateway_internal_url = "https://gw.example"
    settings.gateway_m2m_token = "m2m"
    settings.product_slug = "smart-image-to-cell"
    settings.resolve_webhook_callback_url.return_value = (
        "https://gw.example/webhook/smartsheet/smart-image-to-cell/org-1"
    )
    settings.webhook_callback_ownership_patterns.return_value = [
        "https://gw.example/webhook/smartsheet/smart-image-to-cell/"
    ]

    webhook = MagicMock()
    webhook.id = 999
    webhook.shared_secret = "sec-abc"

    service = MagicMock()
    service.register_webhook = AsyncMock(return_value=webhook)

    repo = MagicMock()
    repo.get_by_worksheet_all_orgs.return_value = []
    repo.save = MagicMock()

    push = AsyncMock()

    with (
        patch("src.config.settings.get_settings", return_value=settings),
        patch(
            "src.services.smartsheet.client.get_smartsheet_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.services.smartsheet.webhooks.SmartsheetWebhookService",
            return_value=service,
        ),
        patch(
            "src.services.supabase.webhook_repo.WebhookRepository",
            return_value=repo,
        ),
        patch("src.adapters.gateway.client.push_webhook_secrets", push),
        patch("src.adapters.gateway.push_webhook_secrets", push),
    ):
        ok = await _register_webhook_for_config(
            worksheet_id=111,
            org_id="org-1",
            config_name="local",
            owner_user_id="user-1",
        )

    assert ok is True
    repo.save.assert_called_once()
    push.assert_awaited_once()
    assert push.await_args.kwargs["secrets"] == {"999": "sec-abc"}
