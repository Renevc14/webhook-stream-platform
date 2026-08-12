"""Gateway m2m client — secret push + ensure-registration (Pulse cutover)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from smarthub_core.webhooks.secret_cache import DEFAULT_PROVIDER

log = logging.getLogger(__name__)


async def push_webhook_secrets(
    *,
    gateway_base_url: str,
    m2m_token: str,
    tool_slug: str,
    organization_id: str,
    secrets: dict[str, str],
    provider: str = DEFAULT_PROVIDER,
) -> None:
    """Seed the gateway Redis cache right after a tool-owned register."""
    if not gateway_base_url:
        log.info("[GATEWAY] URL empty — skip secret push (local)")
        return
    url = (
        f"{gateway_base_url.rstrip('/')}"
        f"/internal/v1/webhook-secrets/{provider}/{tool_slug}/{organization_id}"
    )
    headers = {"Authorization": f"Bearer {m2m_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.put(url, headers=headers, json={"secrets": secrets})
    if resp.status_code >= 300:
        log.error(
            "[GATEWAY] secret push failed status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        raise RuntimeError(f"gateway secret push failed: {resp.status_code}")
    log.info(
        "[GATEWAY] Pushed %s webhook secrets for org=%s",
        len(secrets),
        organization_id,
    )


async def ensure_webhook_registration(
    *,
    gateway_base_url: str,
    m2m_token: str,
    tool_slug: str,
    organization_id: str,
    worksheet_id: Optional[int] = None,
) -> dict[str, Any]:
    """Ask the gateway registrar to register/heal webhooks for this org/sheet."""
    if not gateway_base_url:
        raise RuntimeError("gateway_internal_url is required for ensure-registration")
    url = (
        f"{gateway_base_url.rstrip('/')}"
        f"/internal/v1/lifecycle/ensure-registration"
    )
    headers = {"Authorization": f"Bearer {m2m_token}"}
    payload: dict[str, Any] = {
        "tool_slug": tool_slug,
        "org_id": organization_id,
    }
    if worksheet_id is not None:
        payload["worksheet_id"] = int(worksheet_id)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code >= 300:
        log.error(
            "[GATEWAY] ensure-registration failed status=%s body=%s",
            resp.status_code,
            resp.text[:300],
        )
        raise RuntimeError(
            f"gateway ensure-registration failed: {resp.status_code}"
        )
    data = resp.json() if resp.content else {}
    log.info(
        "[GATEWAY] ensure-registration ok org=%s sheet=%s result=%s",
        organization_id,
        worksheet_id,
        data,
    )
    return data if isinstance(data, dict) else {"raw": data}
