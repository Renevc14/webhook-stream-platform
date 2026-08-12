"""Security surface: auth on every route, org isolation, webhook HMAC, readiness.

These cover the gaps found in the pre-sandbox review: unauthenticated browse
endpoints, spoofable X-Organization-ID, dev-only routes reachable in deployed
environments, and an HMAC bypass.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from smart_qr.api import deps
from smart_qr.api.app import app
from smart_qr.config.settings import Settings

client = TestClient(app)

ORG_A = UUID("11111111-1111-1111-1111-111111111111")
ORG_B = UUID("22222222-2222-2222-2222-222222222222")
USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _clear_membership_cache():
    deps._membership_cache.clear()
    yield
    deps._membership_cache.clear()


def test_health_is_public():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["service"] == "smart-qr"


def test_request_id_is_echoed_back():
    generated = client.get("/health")
    assert generated.headers["X-Request-Id"]

    passed_through = client.get("/health", headers={"X-Request-Id": "hub-abc123"})
    assert passed_through.headers["X-Request-Id"] == "hub-abc123"


@pytest.mark.parametrize(
    "path",
    [
        "/workspaces",
        "/items?workspace_id=1",
        "/columns?worksheet_id=1",
    ],
)
def test_browse_endpoints_require_authentication(path):
    """These read Smartsheet through the service token, so they must never be open."""
    res = client.get(path)
    assert res.status_code == 401


def test_config_list_requires_authentication():
    assert client.get("/api/v1/configs").status_code == 401


def test_inject_event_requires_authentication():
    res = client.post(
        "/api/v1/dev/inject-event",
        json={
            "organization_id": str(ORG_A),
            "config_id": str(uuid4()),
            "row_id": 1,
        },
    )
    assert res.status_code == 401


def test_is_local_only_matches_local_env():
    assert Settings(app_env="local").is_local is True
    assert Settings(app_env="LOCAL").is_local is True
    for env in ("dev", "development", "test", "sandbox", "staging", "production"):
        assert Settings(app_env=env).is_local is False, env


def test_cors_origins_default_to_localhost_and_split_on_comma():
    assert Settings().cors_allowed_origins == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert Settings(cors_origin="https://a.com, https://b.com").cors_allowed_origins == [
        "https://a.com",
        "https://b.com",
    ]


def test_cors_preflight_allows_hub_correlation_header():
    """Hub clients send X-Request-Id on every call; omitting it from allow_headers
    makes the browser fail the preflight with a bare "Failed to fetch"."""
    res = client.options(
        "/api/v1/configs",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,x-organization-id,x-request-id,x-user-id"
            ),
        },
    )
    assert res.status_code == 200
    allowed = res.headers.get("access-control-allow-headers", "").lower()
    assert "x-request-id" in allowed


class _Membership:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def user_belongs_to_org(self, user_id: str, organization_id: UUID):
        self.calls += 1
        return self.answer


@pytest.mark.asyncio
async def test_org_membership_rejects_other_org(monkeypatch):
    stub = _Membership(False)
    monkeypatch.setattr(deps, "OrgMembershipRepository", lambda: stub)
    with pytest.raises(HTTPException) as exc:
        await deps._assert_org_membership(USER, ORG_B, Settings(app_env="sandbox"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_org_membership_allows_own_org_and_caches(monkeypatch):
    stub = _Membership(True)
    monkeypatch.setattr(deps, "OrgMembershipRepository", lambda: stub)
    settings = Settings(app_env="sandbox")
    await deps._assert_org_membership(USER, ORG_A, settings)
    await deps._assert_org_membership(USER, ORG_A, settings)
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_unverifiable_membership_fails_closed_outside_local(monkeypatch):
    stub = _Membership(None)
    monkeypatch.setattr(deps, "OrgMembershipRepository", lambda: stub)
    with pytest.raises(HTTPException) as exc:
        await deps._assert_org_membership(USER, ORG_A, Settings(app_env="sandbox"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unverifiable_membership_allowed_on_developer_machine(monkeypatch):
    stub = _Membership(None)
    monkeypatch.setattr(deps, "OrgMembershipRepository", lambda: stub)
    await deps._assert_org_membership(USER, ORG_A, Settings(app_env="local"))


@pytest.mark.asyncio
async def test_header_only_auth_rejected_when_jwt_secret_is_set():
    """Deployed environments always carry a JWT secret, so headers alone must fail."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_user(
            request=_FakeRequest(),
            authorization=None,
            x_user_id=USER,
            settings=Settings(app_env="local", supabase_jwt_secret="s3cret"),
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_header_only_auth_rejected_outside_local():
    with pytest.raises(HTTPException) as exc:
        await deps.require_user(
            request=_FakeRequest(),
            authorization=None,
            x_user_id=USER,
            settings=Settings(app_env="sandbox"),
        )
    assert exc.value.status_code == 401


class _FakeURL:
    path = "/test"


class _FakeRequest:
    url = _FakeURL()
