"""Credential resolver: SmartQR's error taxonomy over smarthub_core.

The broker HTTP contract lives in smarthub_core and is tested there. What this
file pins is the translation: which core exception becomes which SmartQR
exception, and which resolver ``build_credential_resolver`` picks for a given
environment. Getting that mapping wrong is how a broker blip tells a connected
user to reconnect.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)
from smart_qr.adapters.smartsheet import credentials as cred_mod
from smart_qr.adapters.smartsheet.credentials import (
    CoreCredentialResolver,
    CredentialsMissing,
    CredentialsMisconfigured,
    CredentialsRevoked,
    CredentialsTemporarilyUnavailable,
    StaticCredentialResolver,
    UnconfiguredCredentialResolver,
    build_credential_resolver,
)

ORG = UUID("11111111-1111-1111-1111-111111111111")
USER = "22222222-2222-2222-2222-222222222222"


class FakeCoreResolver:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []
        self.invalidated: list[tuple[str, str]] = []

    async def resolve(self, org_id, product_slug, *, force_refresh=False, user_id=None):
        self.calls.append(
            {
                "org_id": org_id,
                "product_slug": product_slug,
                "force_refresh": force_refresh,
                "user_id": user_id,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def invalidate(self, org_id, product_slug):
        self.invalidated.append((org_id, product_slug))


@pytest.fixture
def settings():
    return SimpleNamespace(
        product_slug="smart-qr",
        encrypt_tool_internal_url="http://encrypt-tool.internal",
        smartsheet_broker_secret="secret",
        smartsheet_access_token="local-pat",
        app_env="staging",
        is_local=False,
    )


@pytest.fixture
def resolver(settings, monkeypatch):
    core = FakeCoreResolver(
        ResolvedToken(
            access_token="oauth-token",
            base_url="https://api.smartsheet.eu/2.0",
            metadata={"source": "oauth", "oauth_state": "active", "region": "EU"},
        )
    )

    async def _fake_get(_settings):
        return core

    monkeypatch.setattr(cred_mod, "get_token_resolver", _fake_get, raising=False)
    # credentials imports get_token_resolver from core_infra lazily
    import smart_qr.adapters.core_infra as core_infra

    monkeypatch.setattr(core_infra, "get_token_resolver", _fake_get)
    monkeypatch.setattr(core_infra, "forget_smartsheet_client", lambda *_a, **_k: None)
    return CoreCredentialResolver(settings), core


@pytest.mark.asyncio
async def test_resolve_maps_core_token_to_credentials(resolver):
    wrapper, core = resolver
    credentials = await wrapper.resolve(ORG, USER)

    assert credentials.access_token == "oauth-token"
    assert credentials.base_url == "https://api.smartsheet.eu/2.0"
    assert credentials.source == "oauth"
    assert credentials.oauth_state == "active"
    assert credentials.metadata["region"] == "EU"
    assert core.calls[0]["product_slug"] == "smart-qr"
    assert core.calls[0]["user_id"] == USER


@pytest.mark.asyncio
async def test_missing_maps_to_credentials_missing(resolver):
    wrapper, core = resolver
    core.outcome = TokenNotFoundError("no credentials")

    with pytest.raises(CredentialsMissing):
        await wrapper.resolve(ORG, USER)


@pytest.mark.asyncio
async def test_revoked_maps_to_credentials_revoked(resolver):
    wrapper, core = resolver
    core.outcome = TokenRevokedError(
        "revoked", oauth_state="revoked", reason="CONNECTION_REVOKED"
    )

    with pytest.raises(CredentialsRevoked) as exc:
        await wrapper.resolve(ORG, USER)

    assert exc.value.oauth_state == "revoked"
    assert exc.value.reason == "CONNECTION_REVOKED"


@pytest.mark.asyncio
async def test_temporary_maps_to_credentials_temporarily_unavailable(resolver):
    wrapper, core = resolver
    core.outcome = TokenTemporarilyUnavailableError("broker down")

    with pytest.raises(CredentialsTemporarilyUnavailable):
        await wrapper.resolve(ORG, USER)


@pytest.mark.asyncio
async def test_unexpected_failure_is_treated_as_temporary(resolver):
    wrapper, core = resolver
    core.outcome = RuntimeError("redis exploded")

    with pytest.raises(CredentialsTemporarilyUnavailable):
        await wrapper.resolve(ORG, USER)


@pytest.mark.asyncio
async def test_invalidate_forwards_to_core(resolver):
    wrapper, core = resolver
    await wrapper.invalidate(ORG, USER)
    assert core.invalidated == [(str(ORG), "smart-qr")]


def test_build_picks_core_when_broker_is_configured(settings):
    resolver = build_credential_resolver(settings)
    assert isinstance(resolver, CoreCredentialResolver)


def test_build_picks_static_only_in_local(settings):
    settings.encrypt_tool_internal_url = ""
    settings.smartsheet_broker_secret = ""
    settings.app_env = "local"
    settings.is_local = True
    resolver = build_credential_resolver(settings)
    assert isinstance(resolver, StaticCredentialResolver)


def test_build_fails_closed_outside_local_without_broker(settings):
    settings.encrypt_tool_internal_url = ""
    settings.smartsheet_broker_secret = ""
    resolver = build_credential_resolver(settings)
    assert isinstance(resolver, UnconfiguredCredentialResolver)


@pytest.mark.asyncio
async def test_unconfigured_resolver_refuses():
    resolver = UnconfiguredCredentialResolver("staging")
    with pytest.raises(CredentialsMisconfigured):
        await resolver.resolve(ORG, USER)


@pytest.mark.asyncio
async def test_static_resolver_returns_the_token():
    resolver = StaticCredentialResolver("local-pat")
    credentials = await resolver.resolve(ORG, USER)
    assert credentials.access_token == "local-pat"
    assert credentials.source == "static"
