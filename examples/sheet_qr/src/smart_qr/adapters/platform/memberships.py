"""Platform membership lookup through the Supabase REST API (service role).

`user_organization_memberships` and `user_profiles` belong to the Hub and are
guarded by RLS policies written for browser sessions (`auth.uid()`). SmartQR
connects to Postgres as a plain role, so those policies make every row invisible:
the membership check would then see "no rows" and reject legitimate callers with
403. Reading the two tables through PostgREST with the service role key bypasses
RLS for this single check — the same route image-to-cell-3 takes — while every
`smartqr_*` table stays behind RLS and asyncpg.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

import httpx

log = logging.getLogger(__name__)

TIMEOUT_S = 8.0
ATTEMPTS = 3


class SupabaseMembershipReader:
    """Answers "does this user belong to this org?" as the service role.

    `user_belongs_to_org` returns None when the question could not be answered
    (network down, key rejected, table absent). None is not a denial: the caller
    owns that policy.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_role_key: str,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_role_key = service_role_key
        self._timeout_s = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/rest/v1",
                timeout=self._timeout_s,
                headers={
                    "apikey": self._service_role_key,
                    "Authorization": f"Bearer {self._service_role_key}",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def user_belongs_to_org(self, user_id: str, organization_id: UUID) -> Optional[bool]:
        """True/False when the platform can answer, None when the read failed.

        `user_organization_memberships` is the source of truth: the Hub lets a
        user switch active plan while `user_profiles.organization_id` keeps
        pointing at their default org, so the profile alone would reject a
        legitimate switched session. The profile is only a fallback for orgs that
        predate memberships.
        """
        memberships = await self._select(
            "user_organization_memberships",
            {
                "select": "user_id,is_active",
                "user_id": f"eq.{user_id}",
                "organization_id": f"eq.{organization_id}",
                "limit": "1",
            },
        )
        if memberships is None:
            return None
        if memberships:
            if memberships[0].get("is_active") is False:
                log.warning(
                    "Membership is inactive — refusing user=%s org=%s", user_id, organization_id
                )
                return False
            return True

        profiles = await self._select(
            "user_profiles",
            {
                "select": "organization_id",
                "id": f"eq.{user_id}",
                "limit": "1",
            },
        )
        if profiles is None:
            return None
        if not profiles:
            log.warning(
                "No membership and no user_profiles row for user=%s — refusing org=%s",
                user_id,
                organization_id,
            )
            return False
        if str(profiles[0].get("organization_id")) == str(organization_id):
            return True
        log.warning("User %s is not a member of org=%s", user_id, organization_id)
        return False

    async def default_org_for_user(self, user_id: str) -> Optional[UUID]:
        """The org on the user's profile, or None when it cannot be read.

        Used only where the Hub cannot tell us which org it is showing (the
        shared Smartsheet pickers send no org header). It is always an org the
        user belongs to, so it is safe — just possibly not the one they switched
        to in the UI.
        """
        profiles = await self._select(
            "user_profiles",
            {"select": "organization_id", "id": f"eq.{user_id}", "limit": "1"},
        )
        if not profiles:
            return None
        raw = profiles[0].get("organization_id")
        if not raw:
            return None
        try:
            return UUID(str(raw))
        except ValueError:
            log.error("user_profiles.organization_id is not a UUID for user=%s", user_id)
            return None

    async def _select(self, table: str, params: dict[str, str]) -> Optional[list[dict[str, Any]]]:
        """Rows on success, None when the read could not be completed."""
        last_error: Optional[str] = None
        for attempt in range(ATTEMPTS):
            try:
                resp = await self._get_client().get(f"/{table}", params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    return payload if isinstance(payload, list) else []
                last_error = f"HTTP {resp.status_code} {resp.text[:200]}"
                # 4xx is a configuration problem (bad key, missing table): retrying
                # only delays the request, so stop and report it as unknown.
                if resp.status_code < 500:
                    log.error("Platform read of %s rejected: %s", table, last_error)
                    return None
            if attempt < ATTEMPTS - 1:
                delay = 0.2 * (2**attempt)
                log.warning(
                    "Platform read of %s failed (attempt %s, retry in %.1fs): %s",
                    table,
                    attempt + 1,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)
        log.error("Platform read of %s failed after %s attempts: %s", table, ATTEMPTS, last_error)
        return None


_reader: Optional[SupabaseMembershipReader] = None
_reader_configured = False


def get_membership_reader() -> Optional[SupabaseMembershipReader]:
    """The shared reader, or None when no service role credentials are configured.

    None means "no platform to ask" — a standalone local Postgres. Callers fall
    back to the direct SQL check in that case.
    """
    global _reader, _reader_configured
    if not _reader_configured:
        from smart_qr.config.settings import get_settings

        settings = get_settings()
        if settings.supabase_url and settings.supabase_service_role_key:
            _reader = SupabaseMembershipReader(
                base_url=settings.supabase_url,
                service_role_key=settings.supabase_service_role_key,
            )
            log.info("Org membership checks will use the Supabase service role")
        else:
            _reader = None
            # Outside local this is a misconfiguration, not a mode: RLS hides the Hub
            # tables from our role, so every request would 403. Say so loudly at boot.
            report = log.info if settings.is_local else log.error
            report(
                "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — org membership falls back "
                "to direct SQL, which RLS makes unreadable on a platform database (env=%s)",
                settings.app_env,
            )
        _reader_configured = True
    return _reader


async def close_membership_reader() -> None:
    global _reader, _reader_configured
    if _reader is not None:
        await _reader.aclose()
    _reader = None
    _reader_configured = False
