from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from smart_qr.adapters.db.repositories import OrgMembershipRepository
from smart_qr.api.jwt_auth import verify_supabase_jwt
from smart_qr.config.settings import Settings, get_settings

log = logging.getLogger(__name__)

MEMBERSHIP_CACHE_TTL_S = 60.0
_membership_cache: dict[tuple[str, str], tuple[bool, float]] = {}


@dataclass
class AuthContext:
    user_id: str
    organization_id: UUID


async def require_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    settings: Settings = Depends(get_settings),
) -> str:
    """Authenticate the caller without requiring an organization.

    Used by the Smartsheet browse endpoints, which read through the service
    token and are not scoped to our own org-owned rows.
    """
    payload = verify_supabase_jwt(authorization, settings.supabase_jwt_secret)
    if payload is None:
        if settings.is_local and not settings.supabase_jwt_secret:
            if not x_user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authorization required (set SUPABASE_JWT_SECRET or X-User-ID locally)",
                )
            log.warning("Local auth without JWT secret — header-only mode user=%s", x_user_id)
            return x_user_id
        log.warning("Rejected unauthenticated request path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Bearer token",
        )

    sub = str(payload.get("sub") or "")
    if x_user_id and sub and x_user_id != sub:
        log.warning("X-User-ID mismatch header=%s sub=%s", x_user_id, sub)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-ID does not match session",
        )
    return x_user_id or sub


async def _assert_org_membership(user_id: str, org_id: UUID, settings: Settings) -> None:
    """Reject requests whose X-Organization-ID does not belong to the caller.

    The header alone is spoofable, so the org is confirmed against
    `user_organization_memberships` (with `user_profiles` as fallback), read with
    the Supabase service role because those Hub tables are RLS-protected. When the
    answer is unavailable we only continue in a strictly local environment;
    anywhere else the request fails closed.
    """
    key = (user_id, str(org_id))
    cached = _membership_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[1] < MEMBERSHIP_CACHE_TTL_S:
        if not cached[0]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User does not belong to this organization",
            )
        return

    belongs = await OrgMembershipRepository().user_belongs_to_org(user_id, org_id)
    if belongs is None:
        if settings.is_local:
            log.warning(
                "Org membership unverifiable (platform tables unavailable) — allowing in "
                "local user=%s org=%s",
                user_id,
                org_id,
            )
            return
        log.error(
            "Org membership check failed and env is not local — rejecting user=%s org=%s",
            user_id,
            org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization membership could not be verified",
        )

    _membership_cache[key] = (belongs, now)
    if not belongs:
        log.warning("Org mismatch — user=%s requested org=%s", user_id, org_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not belong to this organization",
        )


async def require_browse_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    """Auth for the Smartsheet pickers, which cannot send an org header.

    The shared picker components (`WorkspacePicker`, `WorksheetPicker`,
    `ColumnPicker`) are used by every tool and send only the session headers, but
    browsing now needs an org to resolve the user's OAuth token. So: use the
    header when a caller does send it, otherwise fall back to the org on the
    user's profile. Both are orgs the user belongs to; the fallback simply may
    not be the plan they switched to in the Hub.
    """
    user_id = await require_user(
        request=request,
        authorization=authorization,
        x_user_id=x_user_id,
        settings=settings,
    )
    if x_organization_id:
        try:
            org_id = UUID(x_organization_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-Organization-ID",
            ) from exc
        await _assert_org_membership(user_id, org_id, settings)
        return AuthContext(user_id=user_id, organization_id=org_id)

    org_id = await OrgMembershipRepository().default_org_for_user(user_id)
    if org_id is None:
        log.error("Cannot resolve an organization for browse user=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-ID header required (no default organization found)",
        )
    log.info("Browse without org header — using profile org user=%s org=%s", user_id, org_id)
    return AuthContext(user_id=user_id, organization_id=org_id)


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    """I2C-style Bearer JWT + X-User-ID, with X-Organization-ID verified against the profile."""
    user_id = await require_user(
        request=request,
        authorization=authorization,
        x_user_id=x_user_id,
        settings=settings,
    )
    if not x_organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-ID header required",
        )
    try:
        org_id = UUID(x_organization_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Organization-ID",
        ) from exc

    await _assert_org_membership(user_id, org_id, settings)
    return AuthContext(user_id=user_id, organization_id=org_id)
