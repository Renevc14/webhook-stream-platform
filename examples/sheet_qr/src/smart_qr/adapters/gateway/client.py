"""Push webhook secrets to gateway (extract-friendly; no shared DB reads)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx
from smarthub_core.webhooks.secret_cache import DEFAULT_PROVIDER

log = logging.getLogger(__name__)


async def push_webhook_secrets(
    *,
    gateway_base_url: str,
    m2m_token: str,
    tool_slug: str,
    organization_id: UUID,
    secrets: dict[str, str],
    provider: str = DEFAULT_PROVIDER,
) -> None:
    """Seed the gateway's secret cache right after registering a webhook.

    The gateway also pull-syncs `smartqr_webhooks`, but Smartsheet can deliver
    the first callback seconds after ENABLED — well inside one sync cycle — so
    this push covers the cold-start window (SH-215).
    """
    if not gateway_base_url:
        log.info("Gateway URL empty — skip secret push (local)")
        return
    url = (
        f"{gateway_base_url.rstrip('/')}"
        f"/internal/v1/webhook-secrets/{provider}/{tool_slug}/{organization_id}"
    )
    headers = {"Authorization": f"Bearer {m2m_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.put(url, headers=headers, json={"secrets": secrets})
    if resp.status_code >= 300:
        log.error("Gateway secret push failed status=%s body=%s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"gateway secret push failed: {resp.status_code}")
    log.info("Pushed %s webhook secrets for org=%s", len(secrets), organization_id)
