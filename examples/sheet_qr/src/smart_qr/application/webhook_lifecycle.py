"""Smartsheet webhook registration for SmartQR (SH-209 / SH-215 / SH-206).

Registers callbacks at the shared gateway
(``{base}/webhook/smartsheet/{product_slug}/{org_id}``) and stores the shared
secret AES-GCM-encrypted in ``smartqr_webhooks``.

SmartQR never reads that secret back: it does not receive callbacks, so it
never verifies an HMAC. The gateway decrypts the column itself during its
pull-sync. The push at the end of registration only covers the cold-start
window, since Smartsheet can deliver the first callback inside one sync cycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

from smarthub_core.crypto import CryptoService
from smarthub_core.platforms.smartsheet import (
    gateway_callback_url,
    resolve_webhook_callback_url,
)

from smart_qr.adapters.db.repositories import WebhookRepository
from smart_qr.adapters.gateway.client import push_webhook_secrets
from smart_qr.adapters.smartsheet.credentials import (
    DEFAULT_API_BASE,
    CredentialsUnavailable,
    SmartsheetCredentials,
    get_credential_resolver,
)
from smart_qr.config.settings import Settings

log = logging.getLogger(__name__)


def encrypt_webhook_secret(plaintext: str, key: str) -> str:
    """Encrypt with platform AES-GCM (``APP_CRYPTO_KEY`` base64 32-byte)."""
    return CryptoService(key=key).encrypt(plaintext)


def resolve_smartqr_webhook_callback_url(
    settings: Settings, organization_id: UUID | str
) -> str:
    """Gateway-shaped callback URL for SmartQR (3-segment path)."""
    url = resolve_webhook_callback_url(
        organization_id=str(organization_id),
        gateway_base_url=(settings.smartsheet_webhook_callback_base_url or "").strip()
        or None,
        legacy_callback_url=None,
        product_slug=settings.product_slug,
    )
    if not url:
        raise ValueError(
            "SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL required (public base of the "
            "shared webhook gateway, e.g. https://webhooks.example.com)"
        )
    return url


def _ss_client(token: str, base_url: str = DEFAULT_API_BASE) -> Any:
    import smartsheet

    client = smartsheet.Smartsheet(token, api_base=(base_url or DEFAULT_API_BASE))
    client.errors_as_exceptions(True)
    return client


def _delete_prior_webhooks(token: str, worksheet_id: int, base_url: str = DEFAULT_API_BASE) -> None:
    client = _ss_client(token, base_url)
    existing = client.Webhooks.list_webhooks()
    for wh in existing.data or []:
        try:
            sid = int(getattr(wh, "scope_object_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if sid != worksheet_id:
            continue
        name = str(getattr(wh, "name", "") or "")
        if not name.startswith("smart-qr-"):
            continue
        try:
            log.info(
                "Deleting prior SmartQR webhook id=%s status=%s",
                wh.id,
                getattr(wh, "status", None),
            )
            client.Webhooks.delete_webhook(int(wh.id))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to delete webhook %s: %s", wh.id, exc)


def _create_webhook(
    token: str,
    name: str,
    callback: str,
    worksheet_id: int,
    base_url: str = DEFAULT_API_BASE,
) -> tuple[int, str]:
    import smartsheet

    client = _ss_client(token, base_url)
    body = smartsheet.models.Webhook(
        {
            "name": name,
            "callbackUrl": callback,
            "scope": "sheet",
            "scopeObjectId": worksheet_id,
            "events": ["*.*"],
            "version": 1,
        }
    )
    created = client.Webhooks.create_webhook(body)
    result = created.result
    webhook_id = int(result.id)
    shared_secret = getattr(result, "shared_secret", None) or ""
    return webhook_id, shared_secret


def _enable_webhook(token: str, webhook_id: int, base_url: str = DEFAULT_API_BASE) -> None:
    """Enable webhook. Smartsheet sends the challenge while this HTTP call waits.

    Must run in a worker thread so the FastAPI event loop can answer the challenge.
    """
    import smartsheet

    client = _ss_client(token, base_url)
    client.Webhooks.update_webhook(
        webhook_id,
        smartsheet.models.Webhook({"enabled": True}),
    )


def _get_webhook_status(
    token: str, webhook_id: int, base_url: str = DEFAULT_API_BASE
) -> tuple[str, bool, Optional[str]]:
    client = _ss_client(token, base_url)
    got = client.Webhooks.get_webhook(webhook_id)
    current = got.result if hasattr(got, "result") else got
    status = str(getattr(current, "status", "") or "")
    enabled = bool(getattr(current, "enabled", False))
    details = getattr(current, "disabled_details", None)
    return status, enabled, str(details) if details else None


async def _credentials_for(
    settings: Settings,
    organization_id: UUID,
    user_id: Optional[str],
) -> SmartsheetCredentials:
    """Webhook management needs the ADMIN_WEBHOOKS scope of a real user."""
    return await get_credential_resolver(settings).resolve(organization_id, user_id)


async def register_sheet_webhook(
    *,
    settings: Settings,
    organization_id: UUID,
    worksheet_id: int,
    runner_user_id: Optional[UUID],
    acting_user_id: Optional[str] = None,
    credentials: Optional[SmartsheetCredentials] = None,
) -> dict:
    # The webhook is created by whoever clicks Register, but it will keep firing
    # long after: the runner stored below is the identity that processes rows.
    creds = credentials or await _credentials_for(
        settings,
        organization_id,
        acting_user_id or (str(runner_user_id) if runner_user_id else None),
    )
    token, api_base = creds.access_token, creds.base_url
    callback = resolve_smartqr_webhook_callback_url(settings, organization_id)
    name = f"smart-qr-{organization_id}-{worksheet_id}"

    # All Smartsheet SDK calls are sync — run off the event loop so the challenge
    # POST can be handled by the same uvicorn worker while enable waits.
    await asyncio.to_thread(_delete_prior_webhooks, token, worksheet_id, api_base)
    webhook_id, shared_secret = await asyncio.to_thread(
        _create_webhook, token, name, callback, worksheet_id, api_base
    )

    enc = (
        encrypt_webhook_secret(shared_secret, settings.app_crypto_key)
        if shared_secret
        else ""
    )
    await WebhookRepository().upsert(
        organization_id=organization_id,
        worksheet_id=worksheet_id,
        webhook_id=webhook_id,
        shared_secret_encrypted=enc,
        runner_user_id=runner_user_id,
        callback_url=callback,
    )

    log.info(
        "Enabling webhook id=%s (challenge must be answered on event loop)",
        webhook_id,
    )
    await asyncio.to_thread(_enable_webhook, token, webhook_id, api_base)

    status = "NEW_NOT_VERIFIED"
    disabled_details: Optional[str] = None
    for attempt in range(10):
        await asyncio.sleep(0.5 if attempt == 0 else 1.0)
        status, enabled, disabled_details = await asyncio.to_thread(
            _get_webhook_status, token, webhook_id, api_base
        )
        log.info(
            "Webhook %s poll attempt=%s status=%s enabled=%s details=%s",
            webhook_id,
            attempt,
            status,
            enabled,
            disabled_details,
        )
        if status == "ENABLED" or enabled:
            status = "ENABLED"
            break
        if status.startswith("DISABLED"):
            break

    if settings.gateway_internal_url and shared_secret:
        await push_webhook_secrets(
            gateway_base_url=settings.gateway_internal_url,
            m2m_token=settings.gateway_m2m_token,
            tool_slug=settings.product_slug,
            organization_id=organization_id,
            secrets={str(webhook_id): shared_secret},
        )

    log.info(
        "Registered Smartsheet webhook id=%s callback=%s org=%s sheet=%s status=%s",
        webhook_id,
        callback,
        organization_id,
        worksheet_id,
        status,
    )
    out: dict = {
        "webhook_id": webhook_id,
        "callback_url": callback,
        "status": status,
    }
    if disabled_details:
        out["disabled_details"] = disabled_details
    if status != "ENABLED":
        raise ValueError(
            f"Webhook registered but not ENABLED (status={status}"
            + (f", details={disabled_details}" if disabled_details else "")
            + "). Ensure the gateway public URL is reachable and retry Register webhook."
        )
    return out


async def unregister_sheet_webhook(
    *,
    settings: Settings,
    organization_id: UUID,
    worksheet_id: int,
    acting_user_id: Optional[str] = None,
    credentials: Optional[SmartsheetCredentials] = None,
) -> Optional[int]:
    """Drop our webhook for a sheet so Smartsheet stops calling a dead config.

    Best effort: the DB row is always removed, and the Smartsheet side is
    deleted when credentials can still be resolved. A user who deletes their
    config after disconnecting Smartsheet must not get an error for it.
    """
    webhook_id = await WebhookRepository().delete_for_worksheet(
        organization_id, worksheet_id
    )
    try:
        creds = credentials or await _credentials_for(
            settings, organization_id, acting_user_id
        )
    except CredentialsUnavailable as exc:
        log.warning(
            "No Smartsheet credentials (%s) — webhook %s for sheet %s left registered",
            exc,
            webhook_id,
            worksheet_id,
        )
        return webhook_id
    try:
        await asyncio.to_thread(
            _delete_prior_webhooks, creds.access_token, worksheet_id, creds.base_url
        )
        log.info(
            "Unregistered SmartQR webhook org=%s sheet=%s webhook_id=%s",
            organization_id,
            worksheet_id,
            webhook_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Failed to unregister webhook org=%s sheet=%s: %s",
            organization_id,
            worksheet_id,
            exc,
        )
    return webhook_id


__all__ = [
    "encrypt_webhook_secret",
    "gateway_callback_url",
    "register_sheet_webhook",
    "resolve_smartqr_webhook_callback_url",
    "unregister_sheet_webhook",
]
