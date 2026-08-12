"""Membership check through the Supabase service role (bypasses Hub RLS).

The Hub tables are invisible to our Postgres role, so these reads go over
PostgREST. What matters here: memberships win over the profile, an inactive
membership is a denial, and a failed read is "unknown" (None) rather than a
silent denial — `require_auth` turns unknown into 403 outside local.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from smart_qr.adapters.platform import memberships
from smart_qr.adapters.platform.memberships import (
    SupabaseMembershipReader,
    close_membership_reader,
    get_membership_reader,
)

USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DEFAULT_ORG = UUID("11111111-1111-1111-1111-111111111111")
SWITCHED_ORG = UUID("22222222-2222-2222-2222-222222222222")
FOREIGN_ORG = UUID("33333333-3333-3333-3333-333333333333")


def build_reader(handler) -> SupabaseMembershipReader:
    """A reader whose HTTP calls are served by `handler` instead of the network."""
    reader = SupabaseMembershipReader(
        base_url="https://project.supabase.co",
        service_role_key="service-role-key",
    )
    reader._client = httpx.AsyncClient(
        base_url="https://project.supabase.co/rest/v1",
        transport=httpx.MockTransport(handler),
    )
    return reader


class Platform:
    """Minimal PostgREST stand-in for the two tables we read."""

    def __init__(self, *, memberships_rows=(), profile_org=None, status_by_table=None):
        self.memberships_rows = list(memberships_rows)
        self.profile_org = profile_org
        self.status_by_table = status_by_table or {}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(table)
        forced = self.status_by_table.get(table)
        if forced:
            return httpx.Response(forced, json={"message": "forced failure"})
        if table == "user_organization_memberships":
            user = request.url.params.get("user_id", "")
            org = request.url.params.get("organization_id", "")
            rows = [
                row
                for row in self.memberships_rows
                if f"eq.{row['user_id']}" == user and f"eq.{row['organization_id']}" == org
            ]
            return httpx.Response(200, json=[{k: v for k, v in r.items()} for r in rows])
        if self.profile_org is None:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"organization_id": str(self.profile_org)}])


def membership(org: UUID, *, is_active=True) -> dict:
    return {"user_id": USER, "organization_id": str(org), "is_active": is_active}


@pytest.mark.asyncio
async def test_membership_row_wins_over_the_profile_org():
    """A switched active plan is a member even though the profile points elsewhere."""
    platform = Platform(memberships_rows=[membership(SWITCHED_ORG)], profile_org=DEFAULT_ORG)
    reader = build_reader(platform)
    assert await reader.user_belongs_to_org(USER, SWITCHED_ORG) is True
    assert platform.calls == ["user_organization_memberships"]


@pytest.mark.asyncio
async def test_profile_org_is_accepted_without_a_membership_row():
    platform = Platform(profile_org=DEFAULT_ORG)
    reader = build_reader(platform)
    assert await reader.user_belongs_to_org(USER, DEFAULT_ORG) is True
    assert platform.calls == ["user_organization_memberships", "user_profiles"]


@pytest.mark.asyncio
async def test_foreign_org_is_refused():
    reader = build_reader(Platform(memberships_rows=[membership(DEFAULT_ORG)], profile_org=DEFAULT_ORG))
    assert await reader.user_belongs_to_org(USER, FOREIGN_ORG) is False


@pytest.mark.asyncio
async def test_inactive_membership_is_refused():
    platform = Platform(memberships_rows=[membership(SWITCHED_ORG, is_active=False)])
    reader = build_reader(platform)
    assert await reader.user_belongs_to_org(USER, SWITCHED_ORG) is False
    assert platform.calls == ["user_organization_memberships"]


@pytest.mark.asyncio
async def test_membership_without_is_active_column_is_accepted():
    """Older rows may not carry the flag; absence is not inactivity."""
    row = {"user_id": USER, "organization_id": str(SWITCHED_ORG)}
    reader = build_reader(Platform(memberships_rows=[row]))
    assert await reader.user_belongs_to_org(USER, SWITCHED_ORG) is True


@pytest.mark.asyncio
async def test_missing_profile_row_is_refused():
    reader = build_reader(Platform(profile_org=None))
    assert await reader.user_belongs_to_org(USER, DEFAULT_ORG) is False


@pytest.mark.asyncio
async def test_rejected_key_is_unknown_not_a_denial():
    """A bad service role key must not look like "not a member"."""
    platform = Platform(status_by_table={"user_organization_memberships": 401})
    reader = build_reader(platform)
    assert await reader.user_belongs_to_org(USER, DEFAULT_ORG) is None
    assert platform.calls == ["user_organization_memberships"]


@pytest.mark.asyncio
async def test_server_error_retries_then_reports_unknown():
    platform = Platform(status_by_table={"user_organization_memberships": 503})
    reader = build_reader(platform)
    assert await reader.user_belongs_to_org(USER, DEFAULT_ORG) is None
    assert len(platform.calls) == memberships.ATTEMPTS


@pytest.mark.asyncio
async def test_transport_error_reports_unknown():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    reader = build_reader(explode)
    assert await reader.user_belongs_to_org(USER, DEFAULT_ORG) is None


@pytest.mark.asyncio
async def test_reader_is_absent_without_credentials(monkeypatch):
    """No service role configured: callers fall back to the direct SQL path."""
    await close_membership_reader()

    from smart_qr.config import settings as settings_module

    class FakeSettings:
        supabase_url = ""
        supabase_service_role_key = ""
        app_env = "local"
        is_local = True

    monkeypatch.setattr(settings_module, "get_settings", lambda: FakeSettings())
    try:
        assert get_membership_reader() is None
    finally:
        await close_membership_reader()


@pytest.mark.asyncio
async def test_reader_is_built_from_settings(monkeypatch):
    await close_membership_reader()
    from smart_qr.config import settings as settings_module

    class FakeSettings:
        supabase_url = "https://project.supabase.co/"
        supabase_service_role_key = "service-role-key"
        app_env = "sandbox"
        is_local = False

    monkeypatch.setattr(settings_module, "get_settings", lambda: FakeSettings())
    try:
        reader = get_membership_reader()
        assert isinstance(reader, SupabaseMembershipReader)
        assert reader is get_membership_reader()
        assert reader._base_url == "https://project.supabase.co"
    finally:
        await close_membership_reader()
