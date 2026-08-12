"""Internal m2m routes — tools push webhook secrets / kick registrar."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from gateway.config.settings import get_settings
from gateway.lifecycle.manager import get_active_lifecycle_manager
from gateway.secrets.store import DEFAULT_PROVIDER, WebhookSecretStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/v1", tags=["Internal"])

_secret_store: Optional[WebhookSecretStore] = None


def configure_internal_routes(*, secret_store: WebhookSecretStore) -> None:
    global _secret_store
    _secret_store = secret_store


class SecretsBody(BaseModel):
    secrets: Dict[str, str] = Field(default_factory=dict)


class EnsureRegistrationBody(BaseModel):
    tool_slug: str = Field(..., min_length=1)
    org_id: str = Field(..., min_length=1)
    worksheet_id: Optional[int] = None


def _require_m2m(
    authorization: Optional[str] = Header(None),
) -> None:
    settings = get_settings()
    expected = (settings.m2m_token or "").strip() or (
        __import__("os").environ.get("GATEWAY_M2M_TOKEN") or ""
    ).strip()
    env = (settings.app_env or settings.environment or "local").lower()
    if not expected:
        if env in {"local", "dev", "development"}:
            return
        raise HTTPException(503, detail="m2m token not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, detail="Bearer required")
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(401, detail="invalid m2m token")


@router.put("/webhook-secrets/{provider}/{tool_slug}/{org_id}")
async def put_webhook_secrets(
    provider: str,
    tool_slug: str,
    org_id: str,
    body: SecretsBody,
    _: None = Depends(_require_m2m),
) -> dict:
    """Cold-start push: a tool that just registered a webhook hands us the
    secret so the first callback verifies before the next pull-sync cycle.

    The path mirrors the cache key (`{provider}/{tool}/{org}`). There is no
    2-segment variant: the provider is never implicit.
    """
    if _secret_store is None:
        raise HTTPException(503, detail="secret store not ready")
    normalized = provider.lower().strip() or DEFAULT_PROVIDER
    await _secret_store.put_secrets(
        org_id, tool_slug, body.secrets, provider=normalized
    )
    log.info(
        "webhook_gateway: internal secret upsert provider=%s tool=%s org=%s count=%s",
        normalized,
        tool_slug,
        org_id,
        len(body.secrets),
    )
    return {"status": "ok", "count": len(body.secrets)}


@router.post("/lifecycle/ensure-registration")
async def ensure_registration(
    body: EnsureRegistrationBody,
    _: None = Depends(_require_m2m),
) -> dict:
    """Pulse cutover: tool asks gateway to register/heal webhooks now.

    Tools with ``RUN_LIFECYCLE_POLLER=false`` must not call Smartsheet
    register themselves; they POST here after persisting an active config so
    the registrar owns create + Redis secret merge in one shot.
    """
    manager = get_active_lifecycle_manager()
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lifecycle manager not running",
        )
    result = await manager.ensure_registration(
        tool_slug=body.tool_slug,
        org_id=body.org_id,
        worksheet_id=body.worksheet_id,
    )
    if not result.get("ok"):
        # 409 = known business miss (no configs / tool not registered); 502 = hard fail
        err = str(result.get("error") or "ensure_failed")
        code = (
            status.HTTP_409_CONFLICT
            if err
            in {"no_active_configs", "tool_not_in_registrar", "registrar_disabled"}
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=result)
    return result
