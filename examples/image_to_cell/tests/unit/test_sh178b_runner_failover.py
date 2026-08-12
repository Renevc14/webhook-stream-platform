"""SH-178 part B — TokenRevokedError failover + park."""

from __future__ import annotations

import pytest
from smarthub_core.platforms.shared.token_resolver import (
    TokenNotFoundError,
    TokenRevokedError,
)

from src.services.runner_failover import (
    classify_credential_failure,
    failover_configs,
)


class WrappedSmartsheetError(Exception):
    pass


def test_classify_revoked_via_cause():
    root = TokenRevokedError("revoked", oauth_state="revoked")
    wrapped = WrappedSmartsheetError("fail")
    wrapped.__cause__ = root
    assert classify_credential_failure(wrapped) == "reconnect_required"


def test_classify_missing_via_cause():
    root = TokenNotFoundError("none")
    wrapped = WrappedSmartsheetError("fail")
    wrapped.__cause__ = root
    assert classify_credential_failure(wrapped) == "connection_missing"


def test_classify_generic_returns_none():
    assert classify_credential_failure(RuntimeError("boom")) is None


@pytest.mark.asyncio
async def test_failover_promotes_candidate(monkeypatch):
    calls = {"set": [], "park": []}

    class FakeRepo:
        def set_runner(self, config_id, runner_user_id):
            calls["set"].append((config_id, runner_user_id))

        def mark_needs_connection(self, config_id):
            calls["park"].append(config_id)

    async def fake_find(org, *, exclude_user_ids=()):
        assert "runner-old" in exclude_user_ids
        return "runner-new"

    monkeypatch.setattr(
        "src.services.runner_failover.find_failover_runner",
        fake_find,
    )

    summary = await failover_configs(
        [{"id": "cfg-1"}, {"id": "cfg-2"}],
        organization_id="org-1",
        revoked_runner_user_id="runner-old",
        config_repo=FakeRepo(),
    )
    assert summary["reassigned"] == 2
    assert summary["parked"] == 0
    assert summary["new_runner"] == "runner-new"
    assert calls["set"] == [("cfg-1", "runner-new"), ("cfg-2", "runner-new")]
    assert calls["park"] == []


@pytest.mark.asyncio
async def test_failover_parks_when_no_candidate(monkeypatch):
    calls = {"park": []}

    class FakeRepo:
        def set_runner(self, config_id, runner_user_id):
            raise AssertionError("should not set_runner")

        def mark_needs_connection(self, config_id):
            calls["park"].append(config_id)

    async def fake_find(org, *, exclude_user_ids=()):
        return None

    monkeypatch.setattr(
        "src.services.runner_failover.find_failover_runner",
        fake_find,
    )

    summary = await failover_configs(
        [{"id": "cfg-1"}],
        organization_id="org-1",
        revoked_runner_user_id="runner-old",
        config_repo=FakeRepo(),
    )
    assert summary["reassigned"] == 0
    assert summary["parked"] == 1
    assert summary["new_runner"] is None
    assert calls["park"] == ["cfg-1"]
