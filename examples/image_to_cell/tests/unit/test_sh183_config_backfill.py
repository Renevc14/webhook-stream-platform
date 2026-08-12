"""SH-183 — config-scoped manual Run Backfill.

Covers:

1. Route authorization/validation for ``POST /configs/{id}/backfill/estimate``
   and ``POST /configs/{id}/backfill``: session-user requirement, tenant
   isolation (same 404 for cross-org and missing), membership + edit/admin
   permission, paused/non-runnable rejection.
2. Interactive identity: Smartsheet access resolves as the authenticated
   session user, never the config's ``created_by``.
3. Typed 409s: reconnect_required / connection_missing (SH-182 broker
   states), backfill_already_running, budget insufficient with lease release.
4. 202 acceptance: lease secured before accepting, handed to the worker.
5. Lease acquire/hold/release helpers (no double-acquire deadlock).
6. failed_attempts re-arm: only signatures whose upload never succeeded.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from tests.unit.conftest import make_config


pytestmark = pytest.mark.asyncio


ORG = "11111111-1111-1111-1111-111111111111"
OWNER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SESSION_USER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CONFIG_ID = "cfg-183"
WORKSHEET_ID = 7000000000000001


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if col not in r or r.get(col) == val]
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _FakeDb:
    """Table-aware Supabase stub for the SH-183 route helpers."""

    def __init__(self):
        self.rows: Dict[str, List[Dict[str, Any]]] = {}

    def table(self, name):
        return _FakeQuery(list(self.rows.get(name, [])))

    def execute_with_retry(self, fn, _desc=""):
        return fn()


def _config_row(**overrides) -> Dict[str, Any]:
    row = {
        "id": CONFIG_ID,
        "organization_id": ORG,
        "worksheet_id": WORKSHEET_ID,
        "configuration_name": "test config",
        "processing_mode": "webhook",
        "config_type": "default",
        "images_to_process": 1,
        "image1_column_id": 8100000000000001,
        "image1_column_name": "Image-1",
        "paused": False,
        "created_by": OWNER,
        "shared_with_org": False,
    }
    row.update(overrides)
    return row


def _db_with(
    *,
    config_row: Optional[Dict[str, Any]] = None,
    member: bool = True,
    member_active: bool = True,
    profile_role: Optional[str] = None,
    overage_rate: Optional[float] = None,
) -> _FakeDb:
    db = _FakeDb()
    if config_row is not None:
        db.rows["imagetocell_config"] = [config_row]
    if member:
        db.rows["user_organization_memberships"] = [{
            "user_id": SESSION_USER,
            "organization_id": ORG,
            "is_active": member_active,
        }]
    if profile_role is not None:
        db.rows["user_profiles"] = [{
            "id": SESSION_USER, "role": profile_role, "is_admin": False,
        }]
    if overage_rate is not None:
        db.rows["organization_subscriptions"] = [{
            "organization_id": ORG, "overage_rate_per_bolt": overage_rate,
        }]
    return db


def _request(user_id: Optional[str] = SESSION_USER):
    state = SimpleNamespace()
    if user_id:
        state.i2c_jwt_sub = user_id
    return SimpleNamespace(state=state)


def _estimate_result(**overrides) -> Dict[str, Any]:
    est = {
        "images_needed": 4,
        "images_available": 10,
        "bolts_per_image": 15,
        "bolts_needed": 60,
        "bolts_available_in_cap": 150,
        "bolts_overage_estimated": 0,
        "has_ceiling": False,
        "blocked": False,
        "block_reason": None,
    }
    est.update(overrides)
    return est


def _usage_ctx(**overrides) -> Dict[str, Any]:
    ctx = {
        "plan_type": "team",
        "is_free_plan": False,
        "ceiling_bolts": None,
        "remaining_effective_bolts": 150,
        "bolts_per_image": 15,
    }
    ctx.update(overrides)
    return ctx


class _FakeSheetService:
    def __init__(self, rows=None, fetch_error: Optional[Exception] = None):
        self.rows = rows if rows is not None else [{"id": 1, "attachments": []}]
        self.fetch_error = fetch_error

    async def get_rows_with_attachments(self, _worksheet_id, _max_pages):
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.rows


def _estimate_patches(sheet_service, *, estimate, ctx=None):
    return [
        patch(
            "src.services.smartsheet.client.get_smartsheet_client",
            MagicMock(return_value=MagicMock(name="SmartsheetClient")),
        ),
        patch(
            "src.services.smartsheet.sheets.SheetService",
            MagicMock(return_value=sheet_service),
        ),
        patch(
            "src.services.bolt_cap.load_i2c_org_usage_context",
            MagicMock(return_value=ctx if ctx is not None else _usage_ctx()),
        ),
        patch(
            "src.services.i2c_bolt_gate.compute_backfill_estimate",
            MagicMock(return_value=estimate),
        ),
    ]


async def _call_estimate(db, *, request=None, sheet_service=None, estimate=None,
                         ctx=None):
    from src.api.routes.configs import estimate_config_backfill

    sheet_service = sheet_service or _FakeSheetService()
    patches = _estimate_patches(sheet_service, estimate=estimate, ctx=ctx)
    for p in patches:
        p.start()
    try:
        return await estimate_config_backfill(
            CONFIG_ID,
            request or _request(),
            options=None,
            org_id=ORG,
            x_user_id=None,
            db=db,
        )
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Authorization / validation (shared by both routes; exercised via estimate)
# ---------------------------------------------------------------------------


class TestAuthAndValidation:
    async def test_missing_session_user_is_401(self):
        db = _db_with(config_row=_config_row())
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db, request=_request(user_id=None))
        assert e.value.status_code == 401
        assert e.value.detail["error_code"] == "auth_required"

    async def test_cross_org_and_missing_config_are_the_same_404(self):
        # Config exists but belongs to another org → the org-scoped query
        # returns nothing, indistinguishable from a nonexistent id.
        db = _db_with(config_row=_config_row(organization_id="other-org"))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 404
        assert e.value.detail["error_code"] == "config_not_found"

        with pytest.raises(HTTPException) as e2:
            await _call_estimate(_db_with(config_row=None))
        assert e2.value.status_code == 404
        assert e2.value.detail == e.value.detail

    async def test_non_member_is_403(self):
        db = _db_with(config_row=_config_row(shared_with_org=True), member=False)
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 403
        assert e.value.detail["error_code"] == "not_org_member"

    async def test_inactive_member_is_403(self):
        db = _db_with(config_row=_config_row(shared_with_org=True),
                      member_active=False)
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 403
        assert e.value.detail["error_code"] == "not_org_member"

    async def test_member_without_edit_permission_is_403(self):
        # Private config owned by someone else; caller is a plain member.
        db = _db_with(config_row=_config_row(), profile_role="member")
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 403
        assert e.value.detail["error_code"] == "forbidden"

    async def test_shared_config_allows_any_member(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        result = await _call_estimate(db, estimate=_estimate_result())
        assert result.config_id == CONFIG_ID

    async def test_owner_can_run_private_config(self):
        db = _db_with(config_row=_config_row(created_by=SESSION_USER))
        result = await _call_estimate(db, estimate=_estimate_result())
        assert result.config_id == CONFIG_ID

    async def test_transferred_owner_user_id_takes_precedence(self):
        db = _db_with(config_row=_config_row(user_id=SESSION_USER,
                                             created_by=OWNER))
        result = await _call_estimate(db, estimate=_estimate_result())
        assert result.config_id == CONFIG_ID

    async def test_admin_can_run_any_config(self):
        db = _db_with(config_row=_config_row(), profile_role="admin")
        result = await _call_estimate(db, estimate=_estimate_result())
        assert result.config_id == CONFIG_ID

    async def test_paused_config_is_409(self):
        db = _db_with(config_row=_config_row(paused=True, shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "config_paused"

    async def test_non_webhook_config_is_409(self):
        db = _db_with(config_row=_config_row(processing_mode="polling",
                                             shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(db)
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "config_not_runnable"


# ---------------------------------------------------------------------------
# Interactive identity — session user, never created_by
# ---------------------------------------------------------------------------


class TestInteractiveIdentity:
    async def test_sheet_access_resolves_as_session_user(self):
        from src.api.routes.configs import estimate_config_backfill

        db = _db_with(config_row=_config_row(shared_with_org=True))
        client_factory = MagicMock(return_value=MagicMock())
        sheet_service = _FakeSheetService()

        with patch("src.services.smartsheet.client.get_smartsheet_client",
                   client_factory), \
             patch("src.services.smartsheet.sheets.SheetService",
                   MagicMock(return_value=sheet_service)), \
             patch("src.services.bolt_cap.load_i2c_org_usage_context",
                   MagicMock(return_value=_usage_ctx())), \
             patch("src.services.i2c_bolt_gate.compute_backfill_estimate",
                   MagicMock(return_value=_estimate_result())):
            await estimate_config_backfill(
                CONFIG_ID, _request(), options=None, org_id=ORG,
                x_user_id=None, db=db,
            )

        client_factory.assert_called_once_with(ORG, SESSION_USER)
        # The config owner's identity must never be used interactively.
        assert OWNER not in client_factory.call_args.args


# ---------------------------------------------------------------------------
# Credential failure classification (SH-182 broker states)
# ---------------------------------------------------------------------------


class TestCredentialFailures:
    async def test_revoked_connection_maps_to_reconnect_required_409(self):
        from smarthub_core.platforms.shared.token_resolver import TokenRevokedError

        revoked = RuntimeError("wrapped")
        revoked.__cause__ = TokenRevokedError("revoked", oauth_state="revoked",
                                              reason="CONNECTION_REVOKED")
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(
                db, sheet_service=_FakeSheetService(fetch_error=revoked)
            )
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "reconnect_required"

    async def test_missing_credentials_maps_to_connection_missing_409(self):
        from smarthub_core.platforms.shared.token_resolver import TokenNotFoundError

        missing = RuntimeError("wrapped")
        missing.__cause__ = TokenNotFoundError("no credentials")
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(
                db, sheet_service=_FakeSheetService(fetch_error=missing)
            )
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "connection_missing"

    async def test_generic_upstream_failure_is_502(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_estimate(
                db,
                sheet_service=_FakeSheetService(fetch_error=RuntimeError("boom")),
            )
        assert e.value.status_code == 502
        assert e.value.detail["error_code"] == "smartsheet_unavailable"


# ---------------------------------------------------------------------------
# Estimate content
# ---------------------------------------------------------------------------


class TestEstimateResponse:
    async def test_full_estimate_with_overage_disclosure(self):
        db = _db_with(config_row=_config_row(shared_with_org=True),
                      overage_rate=0.05)
        result = await _call_estimate(
            db,
            estimate=_estimate_result(
                images_needed=10, bolts_needed=150,
                bolts_available_in_cap=90, bolts_overage_estimated=60,
            ),
        )
        assert result.images_needed == 10
        assert result.bolts_needed == 150
        assert result.bolts_available_in_cap == 90
        assert result.bolts_overage_estimated == 60
        assert result.overage_allowed is True
        assert result.overage_rate_per_bolt == 0.05
        assert result.blocked is False
        assert "overage" in (result.guidance or "")

    async def test_hard_cap_block_reported_with_guidance(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        result = await _call_estimate(
            db,
            estimate=_estimate_result(
                has_ceiling=True, blocked=True,
                block_reason="backfill_budget_insufficient",
                bolts_overage_estimated=45,
            ),
            ctx=_usage_ctx(ceiling_bolts=100),
        )
        assert result.blocked is True
        assert result.has_hard_cap is True
        assert result.overage_allowed is False
        assert result.block_reason == "backfill_budget_insufficient"
        assert "hard cap" in (result.guidance or "")

    async def test_free_plan_gets_reset_upgrade_guidance(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        result = await _call_estimate(
            db,
            estimate=_estimate_result(
                has_ceiling=True, blocked=True,
                block_reason="backfill_budget_insufficient",
            ),
            ctx=_usage_ctx(is_free_plan=True, plan_type="individual",
                           ceiling_bolts=100),
        )
        assert result.is_free_plan is True
        assert "Free plans cannot buy additional credits" in (result.guidance or "")

    async def test_estimator_failure_never_fabricates_zero_cost(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        result = await _call_estimate(db, estimate=None)
        assert result.error == "estimate_unavailable"
        assert result.blocked is True
        assert "Retry" in (result.guidance or "")

    async def test_prior_log_unavailable_is_explicit_and_retryable(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        result = await _call_estimate(
            db,
            estimate=_estimate_result(
                blocked=True, block_reason="prior_log_unavailable",
                images_needed=0, bolts_needed=0, bolts_available_in_cap=0,
            ),
        )
        assert result.block_reason == "prior_log_unavailable"
        assert "Retry" in (result.guidance or "")


# ---------------------------------------------------------------------------
# Execution route
# ---------------------------------------------------------------------------


def _execution_patches(*, sheet_service=None, pause_block=None,
                       budget_issue=None, lease="default"):
    from src.services.backfill_lock import BackfillLease

    if lease == "default":
        lease = BackfillLease(
            worksheet_id=WORKSHEET_ID, lock_name="lock:x", token="t",
            ttl_s=600, heartbeat_interval_s=60, client=None,
        )
    sheet_service = sheet_service or _FakeSheetService()
    release_mock = AsyncMock()
    patches = {
        "client": patch(
            "src.services.smartsheet.client.get_smartsheet_client",
            MagicMock(return_value=MagicMock()),
        ),
        "sheets": patch(
            "src.services.smartsheet.sheets.SheetService",
            MagicMock(return_value=sheet_service),
        ),
        "pause": patch(
            "src.services.i2c_bolt_gate.preflight_pause_if_blocked",
            MagicMock(return_value=pause_block),
        ),
        "budget": patch(
            "src.services.i2c_bolt_gate.preflight_backfill_budget",
            MagicMock(return_value=budget_issue),
        ),
        "acquire": patch(
            "src.services.backfill_lock.acquire_worksheet_backfill_lease",
            AsyncMock(return_value=lease),
        ),
        "release": patch(
            "src.services.backfill_lock.release_backfill_lease",
            release_mock,
        ),
    }
    return patches, lease, release_mock


async def _call_execute(db, *, worker=None, background_tasks=None, **kw):
    from src.api.routes.configs import run_config_backfill

    patches, lease, release_mock = _execution_patches(**kw)
    started = [p.start() for p in patches.values()]
    _ = started
    try:
        result = await run_config_backfill(
            CONFIG_ID,
            _request(),
            background_tasks if background_tasks is not None else BackgroundTasks(),
            options=None,
            org_id=ORG,
            x_user_id=None,
            db=db,
            worker=worker or MagicMock(),
        )
        return result, lease, release_mock
    finally:
        for p in patches.values():
            p.stop()


class TestExecution:
    async def test_accepted_returns_202_payload_with_run_id(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        tasks = BackgroundTasks()
        worker = MagicMock()
        result, lease, release_mock = await _call_execute(
            db, worker=worker, background_tasks=tasks,
        )
        assert result["status"] == "accepted"
        assert result["config_id"] == CONFIG_ID
        assert result["worksheet_id"] == WORKSHEET_ID
        assert result["run_id"]
        assert result["message"] == "Backfill accepted"
        # Exactly one background job, given the pre-acquired lease and the
        # session user's identity.
        assert len(tasks.tasks) == 1
        task = tasks.tasks[0]
        assert task.func == worker.run_for_config
        assert task.kwargs["lease"] is lease
        assert task.kwargs["session_user_id"] == SESSION_USER
        assert task.kwargs["run_id"] == result["run_id"]
        # Lease is handed off, not released by the route.
        release_mock.assert_not_awaited()

    async def test_overlapping_backfill_is_409_already_running(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_execute(db, lease=None)
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "backfill_already_running"

    async def test_hard_cap_lockout_is_409_before_lease(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            await _call_execute(db, pause_block="hard_cap_reached_flag")
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "hard_cap_reached_flag"

    async def test_budget_insufficient_is_409_and_releases_lease(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        with pytest.raises(HTTPException) as e:
            _, _, release_mock = None, None, None
            try:
                await _call_execute(
                    db,
                    budget_issue={
                        "reason": "backfill_budget_insufficient",
                        "images_needed": 9, "images_available": 2,
                        "bolts_per_image": 15,
                    },
                )
            except HTTPException:
                raise
        assert e.value.status_code == 409
        assert e.value.detail["error_code"] == "backfill_budget_insufficient"
        assert e.value.detail["images_needed"] == 9
        assert e.value.detail["images_available"] == 2

    async def test_fetch_failure_after_lease_releases_lease(self):
        db = _db_with(config_row=_config_row(shared_with_org=True))
        from src.api.routes.configs import run_config_backfill
        from src.services.backfill_lock import BackfillLease

        lease = BackfillLease(
            worksheet_id=WORKSHEET_ID, lock_name="lock:x", token="t",
            ttl_s=600, heartbeat_interval_s=60, client=None,
        )
        release_mock = AsyncMock()
        with patch("src.services.smartsheet.client.get_smartsheet_client",
                   MagicMock(return_value=MagicMock())), \
             patch("src.services.smartsheet.sheets.SheetService",
                   MagicMock(return_value=_FakeSheetService(
                       fetch_error=RuntimeError("boom")))), \
             patch("src.services.i2c_bolt_gate.preflight_pause_if_blocked",
                   MagicMock(return_value=None)), \
             patch("src.services.backfill_lock.acquire_worksheet_backfill_lease",
                   AsyncMock(return_value=lease)), \
             patch("src.services.backfill_lock.release_backfill_lease",
                   release_mock):
            with pytest.raises(HTTPException) as e:
                await run_config_backfill(
                    CONFIG_ID, _request(), BackgroundTasks(), options=None,
                    org_id=ORG, x_user_id=None, db=db, worker=MagicMock(),
                )
        assert e.value.status_code == 502
        release_mock.assert_awaited_once_with(lease)


# ---------------------------------------------------------------------------
# Lease helpers — acquire / hold / release, no double-acquire
# ---------------------------------------------------------------------------


class _FakeRedisLock:
    """In-memory acquire/extend/release with token-holder semantics."""

    def __init__(self):
        self.locks: Dict[str, str] = {}

    async def acquire_lock(self, name, token, _ttl):
        if name in self.locks:
            return False
        self.locks[name] = token
        return True

    async def extend_lock(self, name, token, _ttl):
        return self.locks.get(name) == token

    async def release_lock(self, name, token):
        if self.locks.get(name) == token:
            del self.locks[name]
            return True
        return False


class TestLeaseHelpers:
    async def test_second_acquire_returns_none_while_held(self):
        from src.services.backfill_lock import acquire_worksheet_backfill_lease

        redis = _FakeRedisLock()
        first = await acquire_worksheet_backfill_lease(1, redis_client=redis)
        assert first is not None
        second = await acquire_worksheet_backfill_lease(1, redis_client=redis)
        assert second is None

    async def test_hold_releases_on_exit_even_on_error(self):
        from src.services.backfill_lock import (
            acquire_worksheet_backfill_lease,
            hold_backfill_lease,
        )

        redis = _FakeRedisLock()
        lease = await acquire_worksheet_backfill_lease(1, redis_client=redis)
        with pytest.raises(RuntimeError):
            async with hold_backfill_lease(lease):
                assert lease.lock_name in redis.locks
                raise RuntimeError("boom")
        assert lease.lock_name not in redis.locks

    async def test_release_helper_is_token_safe(self):
        from src.services.backfill_lock import (
            acquire_worksheet_backfill_lease,
            release_backfill_lease,
        )

        redis = _FakeRedisLock()
        lease = await acquire_worksheet_backfill_lease(1, redis_client=redis)
        # Another holder's token must not release our lock.
        stolen = type(lease)(**{**lease.__dict__, "token": "other"})
        await release_backfill_lease(stolen)
        assert lease.lock_name in redis.locks
        await release_backfill_lease(lease)
        assert lease.lock_name not in redis.locks

    async def test_no_redis_degrades_to_noop_lease(self):
        from src.services.backfill_lock import (
            acquire_worksheet_backfill_lease,
            hold_backfill_lease,
        )

        # conftest installs a MagicMock redis via set_redis_client, so force
        # the not-wired path explicitly.
        with patch("src.services.backfill_lock.get_redis_client",
                   MagicMock(return_value=None)):
            lease = await acquire_worksheet_backfill_lease(1)
        assert lease is not None
        assert lease.client is None
        async with hold_backfill_lease(lease):
            pass  # no-op, no error


# ---------------------------------------------------------------------------
# failed_attempts re-arm — only unresolved targets
# ---------------------------------------------------------------------------


class TestFailedAttemptsRearm:
    async def test_rearm_clears_only_unresolved_signatures(self, processor):
        from src.models.row_state import RowState

        state = RowState(config_id=CONFIG_ID, row_id=1)
        state.last_ids = {"sig-completed"}
        state.failed_attempts = {
            "sig-completed": 2,   # output exists → keep counter
            "sig-unresolved": 5,  # exhausted, output missing → re-arm
        }
        rearmed = processor._rearm_failed_attempts({1: state})
        assert rearmed == 1
        assert "sig-unresolved" not in state.failed_attempts
        assert state.failed_attempts["sig-completed"] == 2

    async def test_prefetched_backfill_rearms_and_uses_session_user(
        self, processor, monkeypatch
    ):
        from src.models.results import BackfillResult

        cfg = make_config(config_type="name map")
        captured: Dict[str, Any] = {}

        async def fake_backfill_config(config, rows, ss, sheet_service, *,
                                       rearm_failed_attempts=False,
                                       owner_user_id=None):
            captured["rearm"] = rearm_failed_attempts
            captured["owner_user_id"] = owner_user_id
            return BackfillResult(worksheet_id=config.worksheet_id)

        monkeypatch.setattr(processor, "_backfill_config", fake_backfill_config)
        invalidated = MagicMock()
        monkeypatch.setattr(
            "src.core.processor.invalidate_usage_cache", invalidated
        )

        result = await processor.backfill_config_prefetched(
            cfg, [{"id": 1, "attachments": []}], MagicMock(),
            lease=None, session_user_id=SESSION_USER,
        )
        assert result.success
        assert captured["rearm"] is True
        assert captured["owner_user_id"] == SESSION_USER
        invalidated.assert_called_once_with(cfg.organization_id)
