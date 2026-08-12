"""SH-178 (part B) — explicit runner identity for background work.

With per-user Smartsheet tokens (OAuth rollout), "which token executes
background work" can no longer be an implicit ``created_by`` convention:

  - ``imagetocell_config.runner_user_id`` names the identity explicitly
    (backfilled from ``created_by``; code falls back to ``created_by`` for
    rows written before the backfill).
  - All background paths (processing, webhook registration, DLQ retries,
    lifecycle) resolve tokens as the RUNNER, not the creator.
  - The lifecycle groups an org's configs by runner: Smartsheet's
    list-webhooks endpoint is scoped to the token's USER, so a single
    "first creator" pass cannot see — let alone recover — webhooks owned
    by a different runner's user. Each identity gets its own pass, while
    the expected-sheet set stays ORG-wide so one runner's sheets are never
    "wrong worksheet" zombies in another runner's pass.
  - ``connection_status`` models the parked state ("needs_connection")
    for configs whose runner lost OAuth with no failover candidate.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, patch

import pytest


ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"
CALLBACK = "https://i2c.test/webhook/smartsheet"


# ---------------------------------------------------------------------------
# Factories (mirroring test_sh178_lifecycle_cross_org_cleanup)
# ---------------------------------------------------------------------------


def make_ss_webhook(
    *,
    id: int,
    sheet: int,
    callback_url: str = CALLBACK,
    status: str = "ENABLED",
    enabled: bool = True,
    name: str = "Image-to-Cell - test",
):
    from src.services.smartsheet.webhooks import SmartsheetWebhook

    return SmartsheetWebhook(
        id=id,
        name=name,
        scope_object_id=sheet,
        callback_url=callback_url,
        shared_secret="s3cret",
        enabled=enabled,
        status=status,
    )


def build_manager():
    from src.workers import webhook_lifecycle as lifecycle_module

    manager = lifecycle_module.WebhookLifecycleManager.__new__(
        lifecycle_module.WebhookLifecycleManager
    )
    settings = MagicMock()
    settings.webhook_callback_url = CALLBACK
    settings.webhook_processing_modes_list = ["webhook", "webhook_v3"]
    manager._settings = settings
    manager._db = MagicMock()
    return manager


class FakeWebhookService:
    """Async stand-in for SmartsheetWebhookService with a per-user view."""

    def __init__(self, webhooks: List[Any]):
        self.webhooks = list(webhooks)
        self.deleted_ids: List[int] = []
        self.register_calls: List[Dict[str, Any]] = []

    async def list_webhooks(self, max_age_s: float = 0):
        return [w for w in self.webhooks if w.id not in self.deleted_ids]

    async def get_webhooks_for_worksheet(self, worksheet_id: int):
        return [
            w for w in await self.list_webhooks() if w.scope_object_id == worksheet_id
        ]

    async def delete_webhook(self, webhook_id: int) -> bool:
        self.deleted_ids.append(webhook_id)
        return True

    async def enable_webhook(self, webhook_id: int) -> bool:
        return True

    async def update_callback_url(self, webhook_id: int, new_url: str) -> bool:
        return True

    async def cleanup_duplicate_webhooks(
        self,
        worksheet_id: int,
        keep_webhook_id: Optional[int] = None,
        protected_ids: Optional[Set[int]] = None,
    ) -> int:
        protected_ids = protected_ids or set()
        candidates = [
            w
            for w in await self.get_webhooks_for_worksheet(worksheet_id)
            if w.id not in protected_ids
        ]
        if len(candidates) <= 1:
            return 0
        keep = next(
            (w for w in candidates if w.id == keep_webhook_id), candidates[0]
        )
        deleted = 0
        for w in candidates:
            if w.id != keep.id:
                await self.delete_webhook(w.id)
                deleted += 1
        return deleted

    async def register_webhook(self, **kwargs):
        self.register_calls.append(kwargs)
        return make_ss_webhook(id=999, sheet=kwargs["worksheet_id"])


# ---------------------------------------------------------------------------
# Model — runner / needs_connection
# ---------------------------------------------------------------------------


class TestConfigRunnerModel:
    def _config(self, **overrides):
        from tests.unit.conftest import make_config

        cfg = make_config(images_to_process=1, n_image_columns=1)
        return cfg.model_copy(update=overrides) if overrides else cfg

    def test_runner_prefers_runner_user_id(self):
        cfg = self._config(created_by=USER_A, runner_user_id=USER_B)
        assert cfg.runner == USER_B

    def test_runner_falls_back_to_created_by(self):
        """Rows written before the backfill (or by older code during the
        rollout window) have no runner_user_id — the creator is the runner."""
        cfg = self._config(created_by=USER_A, runner_user_id=None)
        assert cfg.runner == USER_A

    def test_connection_status_defaults_to_active(self):
        cfg = self._config()
        assert cfg.connection_status == "active"
        assert cfg.needs_connection is False

    def test_needs_connection_flag(self):
        cfg = self._config(connection_status="needs_connection")
        assert cfg.needs_connection is True


# ---------------------------------------------------------------------------
# Background processing runs as the runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_row_forwards_runner_as_owner_user_id():
    """Live webhook/DLQ path: the RowProcessor token identity must be the
    config's runner when one is set (SH-179 pinned created_by; part B
    upgrades the contract to runner-with-created_by-fallback)."""
    from src.core.processor import ImageProcessor
    from src.models.results import RowResult

    from tests.unit.conftest import make_config

    config = make_config(images_to_process=1, n_image_columns=1)
    config = config.model_copy(
        update={"created_by": USER_A, "runner_user_id": USER_B}
    )
    row = {"id": 5830930674876292, "attachments": [], "cells": []}

    captured: dict = {}

    class _FakeRowProcessor:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def process(self, *args, **kwargs):
            return RowResult(
                success=True,
                row_id=row["id"],
                images_processed=0,
                images_skipped=0,
                processing_time_ms=1,
            )

    processor = ImageProcessor(supabase=MagicMock(), settings=MagicMock())
    processor._state_repo = MagicMock()

    with patch(
        "src.core.processor.preflight_pause_if_blocked", return_value=None
    ), patch("src.core.processor.RowProcessor", _FakeRowProcessor):
        result = await processor.process_row(config, row)

    assert result.success is True
    assert captured["kwargs"]["owner_user_id"] == USER_B


@pytest.mark.asyncio
async def test_process_row_falls_back_to_created_by_without_runner():
    from src.core.processor import ImageProcessor
    from src.models.results import RowResult

    from tests.unit.conftest import make_config

    config = make_config(images_to_process=1, n_image_columns=1)
    config = config.model_copy(
        update={"created_by": USER_A, "runner_user_id": None}
    )
    row = {"id": 5830930674876292, "attachments": [], "cells": []}

    captured: dict = {}

    class _FakeRowProcessor:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def process(self, *args, **kwargs):
            return RowResult(
                success=True,
                row_id=row["id"],
                images_processed=0,
                images_skipped=0,
                processing_time_ms=1,
            )

    processor = ImageProcessor(supabase=MagicMock(), settings=MagicMock())
    processor._state_repo = MagicMock()

    with patch(
        "src.core.processor.preflight_pause_if_blocked", return_value=None
    ), patch("src.core.processor.RowProcessor", _FakeRowProcessor):
        await processor.process_row(config, row)

    assert captured["kwargs"]["owner_user_id"] == USER_A


# ---------------------------------------------------------------------------
# Lifecycle — one pass per runner identity, org-wide expected sheets
# ---------------------------------------------------------------------------


class TestLifecycleRunnerGrouping:
    def _run(
        self,
        *,
        configs: List[Dict[str, Any]],
        webhooks_by_user: Dict[Optional[str], List[Any]],
        db_rows: List[Dict[str, Any]],
        owners: Dict[int, str],
        active_sheets: Set[int],
        monkeypatch: pytest.MonkeyPatch,
    ):
        from src.workers import webhook_lifecycle as lifecycle_module

        manager = build_manager()

        client_calls: List[tuple] = []
        services: Dict[Optional[str], FakeWebhookService] = {
            user: FakeWebhookService(whs)
            for user, whs in webhooks_by_user.items()
        }

        def _fake_client(org_id, user_id=None):
            client_calls.append((org_id, user_id))
            return user_id  # threaded through to the service factory below

        monkeypatch.setattr(
            lifecycle_module, "get_smartsheet_client", _fake_client
        )
        monkeypatch.setattr(
            lifecycle_module,
            "SmartsheetWebhookService",
            lambda client: services[client],
        )

        db_store = {"rows": list(db_rows)}
        manager._get_db_webhooks_for_org = lambda org_id: [
            r for r in db_store["rows"] if r.get("organization_id") == org_id
        ]

        def _save(webhook, org_id):
            db_store["rows"].append(
                {
                    "webhook_id": str(webhook.id),
                    "worksheet_id": webhook.scope_object_id,
                    "organization_id": org_id,
                }
            )

        manager._save_webhook_to_db = _save
        manager._update_webhook_timestamp = lambda webhook_id: None
        manager._update_webhook_status_in_db = lambda webhook_id, status: None
        manager._delete_webhook_from_db = lambda webhook_id: None

        result = asyncio.run(
            manager._process_organization(
                ORG_A,
                configs,
                global_webhook_owners=owners,
                global_active_sheets=active_sheets,
            )
        )
        return result, services, client_calls

    def test_each_runner_gets_its_own_token_pass(self, monkeypatch):
        """Two runners in one org: the lifecycle must build one webhook
        service per runner (each Smartsheet user only sees its own
        webhooks) and find both configs healthy."""
        wh_a = make_ss_webhook(id=1, sheet=100)  # user A's webhook
        wh_b = make_ss_webhook(id=2, sheet=200)  # user B's webhook

        result, services, client_calls = self._run(
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": USER_A,
                },
                {
                    "id": "cfg-2",
                    "organization_id": ORG_A,
                    "worksheet_id": 200,
                    "configuration_name": "cfg B",
                    # runner takes precedence over created_by
                    "created_by": USER_A,
                    "runner_user_id": USER_B,
                },
            ],
            webhooks_by_user={USER_A: [wh_a], USER_B: [wh_b]},
            db_rows=[
                {"webhook_id": "1", "worksheet_id": 100, "organization_id": ORG_A},
                {"webhook_id": "2", "worksheet_id": 200, "organization_id": ORG_A},
            ],
            owners={1: ORG_A, 2: ORG_A},
            active_sheets={100, 200},
            monkeypatch=monkeypatch,
        )

        users_used = {u for _, u in client_calls}
        assert users_used == {USER_A, USER_B}, "one token pass per runner"
        assert result.webhooks_healthy == 2
        assert services[USER_A].deleted_ids == []
        assert services[USER_B].deleted_ids == []
        assert result.errors == 0

    def test_other_runners_sheet_is_not_a_wrong_worksheet_zombie(
        self, monkeypatch
    ):
        """A webhook registered by user A on a sheet whose config now runs
        as user B (e.g. after a reassign) is visible in A's pass. With
        per-group expected sheets it would be a Type 1 zombie; the
        expected set must stay ORG-wide so it survives."""
        wh_a = make_ss_webhook(id=1, sheet=100)
        # Registered historically by user A for sheet 200 — DB row is ours.
        wh_old = make_ss_webhook(id=4, sheet=200)
        wh_b = make_ss_webhook(id=2, sheet=200)

        result, services, _ = self._run(
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": USER_A,
                },
                {
                    "id": "cfg-2",
                    "organization_id": ORG_A,
                    "worksheet_id": 200,
                    "configuration_name": "cfg B",
                    "created_by": USER_A,
                    "runner_user_id": USER_B,
                },
            ],
            webhooks_by_user={USER_A: [wh_a, wh_old], USER_B: [wh_b]},
            db_rows=[
                {"webhook_id": "1", "worksheet_id": 100, "organization_id": ORG_A},
                {"webhook_id": "4", "worksheet_id": 200, "organization_id": ORG_A},
            ],
            owners={1: ORG_A, 4: ORG_A},
            active_sheets={100, 200},
            monkeypatch=monkeypatch,
        )

        assert 4 not in services[USER_A].deleted_ids, (
            "webhook on another runner's monitored sheet must not be a "
            "wrong-worksheet zombie"
        )
        assert result.zombies_deleted == 0

    def test_configs_without_runner_fall_back_to_created_by_group(
        self, monkeypatch
    ):
        """Legacy rows (runner_user_id missing/None) group under created_by
        — a single pass, exactly like pre-part-B behavior."""
        wh_a = make_ss_webhook(id=1, sheet=100)

        result, _, client_calls = self._run(
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": USER_A,
                },
                {
                    "id": "cfg-2",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A2",
                    "created_by": USER_A,
                    "runner_user_id": None,
                },
            ],
            webhooks_by_user={USER_A: [wh_a]},
            db_rows=[
                {"webhook_id": "1", "worksheet_id": 100, "organization_id": ORG_A},
            ],
            owners={1: ORG_A},
            active_sheets={100},
            monkeypatch=monkeypatch,
        )

        users_used = [u for _, u in client_calls]
        assert set(users_used) == {USER_A}
        assert result.webhooks_checked == 2
        assert result.errors == 0


# ---------------------------------------------------------------------------
# ConfigRepository — set_runner / mark_needs_connection
# ---------------------------------------------------------------------------


class TestConfigRepoRunnerHelpers:
    def _repo(self):
        from src.services.supabase.config_repo import ConfigRepository

        db = MagicMock()
        # execute_with_retry(fn, desc) -> run the query lambda so the
        # builder chain records the update payload.
        db.execute_with_retry.side_effect = lambda fn, desc: fn()
        return ConfigRepository(supabase=db), db

    def test_set_runner_activates_and_reassigns(self):
        repo, db = self._repo()

        repo.set_runner("cfg-1", USER_B)

        payload = db.table.return_value.update.call_args[0][0]
        assert payload["runner_user_id"] == USER_B
        assert payload["connection_status"] == "active"
        db.table.return_value.update.return_value.eq.assert_called_once_with(
            "id", "cfg-1"
        )

    def test_mark_needs_connection_parks_config(self):
        repo, db = self._repo()

        repo.mark_needs_connection("cfg-1")

        payload = db.table.return_value.update.call_args[0][0]
        assert payload["connection_status"] == "needs_connection"
        assert "runner_user_id" not in payload
        db.table.return_value.update.return_value.eq.assert_called_once_with(
            "id", "cfg-1"
        )
