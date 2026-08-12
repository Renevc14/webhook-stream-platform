"""SH-178 — lifecycle zombie cleanup must never delete another org's webhooks.

Smartsheet's ``GET /webhooks`` is scoped to the token's USER, not to our
organization. When the same Smartsheet account backs multiple Smarthub orgs
(the staging incident: two orgs sharing one Smartsheet user), each org's
health-check cycle "sees" the other orgs' webhooks. Before this fix, the
Type 1 (wrong worksheet) zombie scan classified every visible webhook whose
sheet was not in *this* org's configs as a zombie and deleted it — orgs took
turns destroying each other's webhooks every 5 minutes, and the lifecycle
then re-registered its own, producing an endless delete/re-register loop.

The fix gates every deletion on PROVABLE ownership:

  - A webhook is only a Type 1 zombie when its id is registered to this org
    in ``imagetocell_webhooks``, OR when it is in no org's DB and its sheet
    has no active I2C config in ANY org (true orphan).
  - Webhooks provably registered to another org are excluded from all
    cleanup paths: zombie scan, duplicate cleanup, and the register
    pre/post-cleanup (``protected_ids``).
  - If the cross-org ownership lookup fails, the cleanup FAILS CLOSED:
    nothing is deleted that cycle.

These tests exercise the zombie scan, the org-level processing wiring, the
webhook service cleanup guards, and the create-config registration path.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock

import pytest


ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CALLBACK = "https://i2c.test/webhook/smartsheet"


# ---------------------------------------------------------------------------
# Factories
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


def build_manager(
    *,
    callback_url: str = CALLBACK,
    modes: Optional[List[str]] = None,
    gateway_base_url: Optional[str] = None,
    product_slug: str = "smart-image-to-cell",
):
    """WebhookLifecycleManager without __init__ (no real deps needed)."""
    from smarthub_core.platforms.smartsheet import (
        resolve_webhook_callback_url,
        webhook_callback_ownership_patterns,
    )
    from src.workers import webhook_lifecycle as lifecycle_module

    manager = lifecycle_module.WebhookLifecycleManager.__new__(
        lifecycle_module.WebhookLifecycleManager
    )
    settings = MagicMock()
    settings.webhook_callback_url = callback_url
    settings.smartsheet_webhook_callback_base_url = gateway_base_url
    settings.product_slug = product_slug
    settings.webhook_processing_modes_list = modes or ["webhook", "webhook_v3"]
    settings.has_webhook_callback_config = bool(callback_url or gateway_base_url)
    settings.resolve_webhook_callback_url = lambda org_id: resolve_webhook_callback_url(
        organization_id=org_id,
        gateway_base_url=gateway_base_url,
        legacy_callback_url=callback_url,
        product_slug=product_slug,
    )
    settings.webhook_callback_ownership_patterns = (
        lambda: webhook_callback_ownership_patterns(
            gateway_base_url=gateway_base_url,
            legacy_callback_url=callback_url,
            product_slug=product_slug,
        )
    )
    manager._settings = settings
    manager._db = MagicMock()
    return manager


class FakeWebhookService:
    """Async stand-in for SmartsheetWebhookService that records deletions."""

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
# _find_zombie_webhooks — ownership gate
# ---------------------------------------------------------------------------


_UNSET = object()


class TestFindZombiesOwnershipGate:
    def _scan(
        self,
        webhooks,
        *,
        db_webhooks=None,
        expected_sheets=None,
        owners=_UNSET,
        active_sheets=_UNSET,
        org_id=ORG_A,
    ):
        # ``None`` means "lookup failed" (fail closed) and must be passed
        # through; the _UNSET sentinel supplies empty defaults instead.
        manager = build_manager()
        return manager._find_zombie_webhooks(
            smartsheet_webhooks=webhooks,
            db_webhooks=db_webhooks or [],
            expected_sheets=expected_sheets or set(),
            callback_url_pattern=CALLBACK,
            org_id=org_id,
            global_webhook_owners={} if owners is _UNSET else owners,
            global_active_sheets=set() if active_sheets is _UNSET else active_sheets,
        )

    def test_cross_org_webhook_is_never_a_zombie(self):
        """The staging incident: org A sees org B's webhook on a sheet org A
        does not monitor. Before the fix it was Type 1 and got deleted."""
        foreign = make_ss_webhook(id=2001, sheet=200)

        wrong, dups, stale, skipped = self._scan(
            [foreign],
            expected_sheets={100},
            owners={2001: ORG_B},
            active_sheets={100, 200},
        )

        assert wrong == [] and dups == [] and stale == []
        assert skipped == 1

    def test_own_webhook_on_unmonitored_sheet_is_still_deleted(self):
        """Provably ours (this org's DB row) + no config here → real zombie.
        The legitimate cleanup keeps working."""
        ours = make_ss_webhook(id=2002, sheet=300)

        wrong, dups, stale, skipped = self._scan(
            [ours],
            db_webhooks=[{"webhook_id": "2002", "worksheet_id": 300}],
            expected_sheets={100},
            owners={2002: ORG_A},
            active_sheets={100},
        )

        assert [w.id for w in wrong] == [2002]
        assert skipped == 0

    def test_true_orphan_is_deleted_when_no_org_monitors_the_sheet(self):
        """In no org's DB and the sheet has no active config anywhere →
        genuine leftover (e.g. config deleted while webhook survived)."""
        orphan = make_ss_webhook(id=2003, sheet=400)

        wrong, dups, stale, skipped = self._scan(
            [orphan],
            expected_sheets={100},
            owners={},
            active_sheets={100},
        )

        assert [w.id for w in wrong] == [2003]
        assert skipped == 0

    def test_unproven_webhook_on_globally_active_sheet_is_left_alone(self):
        """Not registered to any org yet, but SOME org actively monitors the
        sheet (e.g. another org's lifecycle registered it moments ago and its
        DB row has not synced). Must not be deleted."""
        unproven = make_ss_webhook(id=2004, sheet=500)

        wrong, dups, stale, skipped = self._scan(
            [unproven],
            expected_sheets={100},
            owners={},
            active_sheets={100, 500},
        )

        assert wrong == [] and dups == [] and stale == []
        assert skipped == 1

    def test_fail_closed_when_owners_lookup_missing(self):
        """DB failure on the ownership map → delete NOTHING this cycle, even
        webhooks that would otherwise be provable zombies."""
        ours = make_ss_webhook(id=2005, sheet=300)

        wrong, dups, stale, skipped = self._scan(
            [ours],
            db_webhooks=[{"webhook_id": "2005", "worksheet_id": 300}],
            expected_sheets={100},
            owners=None,
            active_sheets={100},
        )

        assert wrong == [] and dups == [] and stale == [] and skipped == 0

    def test_fail_closed_when_active_sheets_lookup_missing(self):
        ours = make_ss_webhook(id=2006, sheet=300)

        wrong, dups, stale, skipped = self._scan(
            [ours],
            db_webhooks=[{"webhook_id": "2006", "worksheet_id": 300}],
            expected_sheets={100},
            owners={2006: ORG_A},
            active_sheets=None,
        )

        assert wrong == [] and dups == [] and stale == [] and skipped == 0

    def test_foreign_callback_urls_are_ignored_entirely(self):
        """Webhooks pointing elsewhere (V2, other tools) were never in scope
        and stay out of scope."""
        other_tool = make_ss_webhook(
            id=2007, sheet=999, callback_url="https://other.tool/webhook"
        )

        wrong, dups, stale, skipped = self._scan(
            [other_tool],
            expected_sheets={100},
            owners={},
            active_sheets=set(),
        )

        assert wrong == [] and dups == [] and stale == [] and skipped == 0

    def test_cross_org_webhook_on_shared_sheet_is_not_a_duplicate(self):
        """Both orgs monitor the SAME sheet. Org A must not treat org B's
        webhook as a duplicate of its own."""
        mine = make_ss_webhook(id=3001, sheet=100)
        theirs = make_ss_webhook(id=3002, sheet=100)

        wrong, dups, stale, skipped = self._scan(
            [mine, theirs],
            db_webhooks=[{"webhook_id": "3001", "worksheet_id": 100}],
            expected_sheets={100},
            owners={3001: ORG_A, 3002: ORG_B},
            active_sheets={100},
        )

        assert wrong == [] and dups == [] and stale == []
        assert skipped == 1

    def test_own_duplicates_on_monitored_sheet_still_cleaned(self):
        """Duplicate detection for our own webhooks is unchanged: keep the
        DB-registered one, delete the rest."""
        keeper = make_ss_webhook(id=3003, sheet=100)
        dup = make_ss_webhook(id=3004, sheet=100)

        wrong, dups, stale, skipped = self._scan(
            [keeper, dup],
            db_webhooks=[{"webhook_id": "3003", "worksheet_id": 100}],
            expected_sheets={100},
            owners={3003: ORG_A},
            active_sheets={100},
        )

        assert [w.id for w in dups] == [3004]
        assert wrong == [] and stale == []

    def test_stale_detection_on_own_sheet_still_works(self):
        """A single unregistered webhook on a sheet we monitor is still
        flagged stale (pre-fix behaviour preserved for our own sheets)."""
        stale_wh = make_ss_webhook(id=3005, sheet=100)

        wrong, dups, stale, skipped = self._scan(
            [stale_wh],
            db_webhooks=[],
            expected_sheets={100},
            owners={},
            active_sheets={100},
        )

        assert [w.id for w in stale] == [3005]

    def test_result_dict_exposes_unowned_skipped(self):
        from src.workers.webhook_lifecycle import HealthCheckResult

        result = HealthCheckResult(unowned_skipped=3)
        assert result.to_dict()["unowned_skipped"] == 3


# ---------------------------------------------------------------------------
# Global ownership lookups — parsing + fail-closed None on error
# ---------------------------------------------------------------------------


class TestGlobalOwnershipLookups:
    def _db_returning(self, rows):
        db = MagicMock()
        result = MagicMock()
        result.data = rows
        db.execute_with_retry.side_effect = lambda fn, _desc="": result
        return db

    def test_owners_map_parses_string_webhook_ids(self):
        manager = build_manager()
        manager._db = self._db_returning(
            [
                {"webhook_id": "111", "organization_id": ORG_A},
                {"webhook_id": "222", "organization_id": ORG_B},
                {"webhook_id": None, "organization_id": ORG_B},  # ignored
                {"webhook_id": "garbage", "organization_id": ORG_B},  # ignored
            ]
        )

        owners = manager._get_global_webhook_owners()

        assert owners == {111: ORG_A, 222: ORG_B}

    def test_owners_returns_none_on_db_error(self):
        manager = build_manager()
        manager._db.execute_with_retry.side_effect = RuntimeError("supabase down")

        assert manager._get_global_webhook_owners() is None

    def test_active_sheets_parses_and_dedupes(self):
        manager = build_manager()
        manager._db = self._db_returning(
            [
                {"worksheet_id": 100},
                {"worksheet_id": "200"},
                {"worksheet_id": 100},
                {"worksheet_id": None},  # ignored
            ]
        )

        assert manager._get_global_active_sheets() == {100, 200}

    def test_active_sheets_returns_none_on_db_error(self):
        manager = build_manager()
        manager._db.execute_with_retry.side_effect = RuntimeError("supabase down")

        assert manager._get_global_active_sheets() is None

    def test_active_sheets_filters_by_processing_modes(self):
        """The safety net must query the same modes the lifecycle manages."""
        manager = build_manager(modes=["webhook", "webhook_v3"])

        captured: Dict[str, Any] = {}
        chain = MagicMock()
        chain.select.return_value = chain
        chain.eq.return_value = chain

        def _in(field, values):
            captured["in_field"] = field
            captured["in_values"] = list(values)
            return chain

        chain.in_.side_effect = _in
        chain.execute.return_value = MagicMock(data=[])

        db = MagicMock()
        db.table.return_value = chain
        db.execute_with_retry.side_effect = lambda fn, _desc="": fn()
        manager._db = db

        manager._get_global_active_sheets()

        assert captured["in_field"] == "processing_mode"
        assert captured["in_values"] == ["webhook", "webhook_v3"]


# ---------------------------------------------------------------------------
# _process_organization — end-to-end wiring of the ownership gate
# ---------------------------------------------------------------------------


class TestProcessOrganization:
    def _run(
        self,
        *,
        webhooks: List[Any],
        db_rows: List[Dict[str, Any]],
        owners: Optional[Dict[int, str]],
        active_sheets: Optional[Set[int]],
        configs: List[Dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ):
        from src.workers import webhook_lifecycle as lifecycle_module

        manager = build_manager()
        service = FakeWebhookService(webhooks)

        monkeypatch.setattr(
            lifecycle_module, "get_smartsheet_client", lambda *a, **k: MagicMock()
        )
        monkeypatch.setattr(
            lifecycle_module,
            "SmartsheetWebhookService",
            lambda client: service,
        )

        # DB fakes on the instance: org-scoped webhook rows + no-op writes.
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
        return result, service

    def test_cross_org_webhook_survives_full_org_cycle(self, monkeypatch):
        """Org A's full health-check pass with org B's webhook visible:
        org B's webhook is untouched, org A's own zombie is removed."""
        mine_healthy = make_ss_webhook(id=1, sheet=100)
        theirs = make_ss_webhook(id=2, sheet=200)  # org B, sheet not ours
        my_zombie = make_ss_webhook(id=3, sheet=300)  # ours, config deleted

        result, service = self._run(
            webhooks=[mine_healthy, theirs, my_zombie],
            db_rows=[
                {"webhook_id": "1", "worksheet_id": 100, "organization_id": ORG_A},
                {"webhook_id": "3", "worksheet_id": 300, "organization_id": ORG_A},
                {"webhook_id": "2", "worksheet_id": 200, "organization_id": ORG_B},
            ],
            owners={1: ORG_A, 2: ORG_B, 3: ORG_A},
            active_sheets={100, 200},
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": "user-a",
                }
            ],
            monkeypatch=monkeypatch,
        )

        assert 2 not in service.deleted_ids, "cross-org webhook was deleted"
        assert 3 in service.deleted_ids, "own zombie was not cleaned up"
        assert result.webhooks_healthy == 1
        assert result.zombies_deleted == 1
        assert result.unowned_skipped == 1
        assert result.errors == 0

    def test_fail_closed_cycle_deletes_nothing(self, monkeypatch):
        """Ownership context unavailable → zero deletions org-wide."""
        my_zombie = make_ss_webhook(id=3, sheet=300)

        result, service = self._run(
            webhooks=[my_zombie],
            db_rows=[
                {"webhook_id": "3", "worksheet_id": 300, "organization_id": ORG_A},
            ],
            owners=None,
            active_sheets=None,
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": "user-a",
                }
            ],
            monkeypatch=monkeypatch,
        )

        assert service.deleted_ids == []
        assert result.zombies_deleted == 0

    def test_foreign_webhook_on_config_sheet_triggers_register_not_delete(
        self, monkeypatch
    ):
        """Org A has a config on sheet 100 but only org B's webhook exists
        there. Org A must register its OWN webhook with org B's id passed as
        protected — never delete it."""
        theirs_on_my_sheet = make_ss_webhook(id=7, sheet=100)

        result, service = self._run(
            webhooks=[theirs_on_my_sheet],
            db_rows=[
                {"webhook_id": "7", "worksheet_id": 100, "organization_id": ORG_B},
            ],
            owners={7: ORG_B},
            active_sheets={100},
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": "user-a",
                }
            ],
            monkeypatch=monkeypatch,
        )

        assert 7 not in service.deleted_ids
        assert result.webhooks_registered == 1
        assert len(service.register_calls) == 1
        assert service.register_calls[0]["protected_ids"] == {7}

    def test_adopted_webhook_not_deleted_as_stale_same_cycle(self, monkeypatch):
        """Case 6 adopts a healthy-but-unregistered webhook into the DB; the
        zombie scan later in the SAME cycle must see the fresh row (re-fetch)
        instead of flagging it stale and deleting what it just adopted."""
        adopted = make_ss_webhook(id=9, sheet=100)

        result, service = self._run(
            webhooks=[adopted],
            db_rows=[],  # not in DB yet — case 6 saves it
            owners={},
            active_sheets={100},
            configs=[
                {
                    "id": "cfg-1",
                    "organization_id": ORG_A,
                    "worksheet_id": 100,
                    "configuration_name": "cfg A",
                    "created_by": "user-a",
                }
            ],
            monkeypatch=monkeypatch,
        )

        assert service.deleted_ids == []
        assert result.webhooks_healthy == 1
        assert result.stale_deleted == 0


# ---------------------------------------------------------------------------
# SmartsheetWebhookService — protected_ids in cleanup + registration
# ---------------------------------------------------------------------------


class FakeSDKWebhooks:
    """Minimal Smartsheet SDK ``Webhooks`` surface backed by dicts."""

    def __init__(self, webhooks: List[Dict[str, Any]]):
        self.webhooks = {w["id"]: dict(w) for w in webhooks}
        self.deleted_ids: List[int] = []
        self._next_id = 90000

    def list_webhooks(self, page_size: int = 100, page: int = 1):
        response = MagicMock()
        response.data = list(self.webhooks.values()) if page == 1 else []
        return response

    def create_webhook(self, spec):
        self._next_id += 1
        data = {
            "id": self._next_id,
            "name": spec.name if hasattr(spec, "name") else "wh",
            "scopeObjectId": int(spec.scope_object_id),
            "callbackUrl": str(spec.callback_url),
            "sharedSecret": "fresh-secret",
            "enabled": False,
            "status": "NEW_NOT_VERIFIED",
        }
        self.webhooks[data["id"]] = data
        response = MagicMock()
        response.result = data
        return response

    def update_webhook(self, webhook_id: int, spec):
        wh = self.webhooks[webhook_id]
        if getattr(spec, "enabled", None) is not None:
            wh["enabled"] = bool(spec.enabled)
            wh["status"] = "ENABLED" if spec.enabled else "DISABLED_BY_OWNER"
        response = MagicMock()
        response.result = dict(wh)
        return response

    def delete_webhook(self, webhook_id: int):
        self.deleted_ids.append(webhook_id)
        self.webhooks.pop(webhook_id, None)
        return MagicMock()


class FakeSmartsheetClient:
    def __init__(self, sdk_webhooks: FakeSDKWebhooks):
        self._sdk = MagicMock()
        self._sdk.Webhooks = sdk_webhooks

    async def call_with_retry_async(self, fn, description: str = ""):
        return fn(self._sdk)


def _webhook_row(id: int, sheet: int, callback_url: str = CALLBACK) -> Dict[str, Any]:
    return {
        "id": id,
        "name": "Image-to-Cell - test",
        "scopeObjectId": sheet,
        "callbackUrl": callback_url,
        "sharedSecret": "old-secret",
        "enabled": True,
        "status": "ENABLED",
    }


class TestWebhookServiceProtectedIds:
    def _service(self, webhooks: List[Dict[str, Any]]):
        from src.services.smartsheet.webhooks import SmartsheetWebhookService

        sdk = FakeSDKWebhooks(webhooks)
        service = SmartsheetWebhookService(FakeSmartsheetClient(sdk))
        return service, sdk

    def test_cleanup_all_skips_protected(self):
        service, sdk = self._service(
            [_webhook_row(900, 500), _webhook_row(901, 500)]
        )

        deleted = asyncio.run(
            service.cleanup_all_webhooks_for_worksheet(500, protected_ids={900})
        )

        assert deleted == 1
        assert sdk.deleted_ids == [901]

    def test_cleanup_all_without_protected_keeps_legacy_behaviour(self):
        service, sdk = self._service(
            [_webhook_row(900, 500), _webhook_row(901, 500)]
        )

        deleted = asyncio.run(service.cleanup_all_webhooks_for_worksheet(500))

        assert deleted == 2
        assert sorted(sdk.deleted_ids) == [900, 901]

    def test_cleanup_all_skips_foreign_product_callback(self):
        """Cell History (or any non-I2C callback) on a shared sheet must survive."""
        ch_url = "https://cell-history.prodactivelabs.com/webhook/smartsheet"
        service, sdk = self._service(
            [
                _webhook_row(900, 500, callback_url=ch_url),
                _webhook_row(901, 500, callback_url=CALLBACK),
            ]
        )

        deleted = asyncio.run(
            service.cleanup_all_webhooks_for_worksheet(
                500, ownership_patterns=[CALLBACK]
            )
        )

        assert deleted == 1
        assert sdk.deleted_ids == [901]
        assert 900 in sdk.webhooks

    def test_register_does_not_delete_cell_history_webhook(self):
        """Register pre/post cleanup must leave Cell History alone even without
        protected_ids — shared-sheet coexistence for I2C + CH-2."""
        ch_url = "https://cell-history.prodactivelabs.com/webhook/smartsheet"
        service, sdk = self._service(
            [
                _webhook_row(900, 500, callback_url=ch_url),
                _webhook_row(901, 500, callback_url=CALLBACK),
            ]
        )

        webhook = asyncio.run(
            service.register_webhook(
                worksheet_id=500,
                callback_url=CALLBACK,
                name="Image-to-Cell - cfg",
            )
        )

        assert 900 not in sdk.deleted_ids
        assert 901 in sdk.deleted_ids
        assert 900 in sdk.webhooks
        assert webhook.id in sdk.webhooks
        assert webhook.shared_secret == "fresh-secret"

    def test_cleanup_duplicates_never_considers_protected(self):
        """A protected (foreign) webhook is not 'kept' nor 'deleted' — it is
        simply not part of the duplicate set."""
        service, sdk = self._service(
            [
                _webhook_row(900, 500),  # foreign
                _webhook_row(901, 500),  # ours (DB)
                _webhook_row(902, 500),  # our duplicate
            ]
        )

        deleted = asyncio.run(
            service.cleanup_duplicate_webhooks(
                500, keep_webhook_id=901, protected_ids={900}
            )
        )

        assert deleted == 1
        assert sdk.deleted_ids == [902]

    def test_cleanup_duplicates_noop_when_only_foreign_and_one_of_ours(self):
        service, sdk = self._service(
            [_webhook_row(900, 500), _webhook_row(901, 500)]
        )

        deleted = asyncio.run(
            service.cleanup_duplicate_webhooks(
                500, keep_webhook_id=901, protected_ids={900}
            )
        )

        assert deleted == 0
        assert sdk.deleted_ids == []

    def test_register_pre_and_post_cleanup_respect_protected(self):
        """Full registration on a sheet where another org has a webhook:
        pre-cleanup removes only OUR stale webhook, post-cleanup leaves the
        foreign one, and the new webhook comes back with its secret."""
        service, sdk = self._service(
            [
                _webhook_row(900, 500),  # foreign — must survive
                _webhook_row(901, 500),  # our stale — must be removed
            ]
        )

        webhook = asyncio.run(
            service.register_webhook(
                worksheet_id=500,
                callback_url=CALLBACK,
                name="Image-to-Cell - cfg",
                protected_ids={900},
            )
        )

        assert 900 not in sdk.deleted_ids
        assert 901 in sdk.deleted_ids
        assert webhook.id in sdk.webhooks
        assert webhook.shared_secret == "fresh-secret"
        assert 900 in sdk.webhooks, "foreign webhook removed during registration"


# ---------------------------------------------------------------------------
# configs._register_webhook_for_config — create-time protection + fail closed
# ---------------------------------------------------------------------------


class TestRegisterWebhookForConfig:
    @pytest.fixture
    def patched_env(self, monkeypatch):
        """Patch the call-time imports of _register_webhook_for_config."""
        from src.config import settings as settings_module
        from src.services.smartsheet import client as client_module
        from src.services.smartsheet import webhooks as webhooks_module
        from src.services.supabase import webhook_repo as repo_module

        settings = MagicMock()
        settings.webhook_callback_url = CALLBACK
        settings.smartsheet_webhook_callback_base_url = None
        settings.resolve_webhook_callback_url = MagicMock(return_value=CALLBACK)
        monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
        monkeypatch.setattr(
            client_module, "get_smartsheet_client", lambda *a, **k: MagicMock()
        )

        registered: Dict[str, Any] = {}

        class FakeService:
            def __init__(self, client):
                pass

            async def register_webhook(self, **kwargs):
                registered.update(kwargs)
                return make_ss_webhook(id=999, sheet=kwargs["worksheet_id"])

        monkeypatch.setattr(webhooks_module, "SmartsheetWebhookService", FakeService)
        monkeypatch.setattr(
            repo_module.WebhookRepository, "save", lambda self, reg: None
        )
        return registered, repo_module

    def test_foreign_rows_become_protected_ids(self, patched_env, monkeypatch):
        from src.api.routes.configs import _register_webhook_for_config

        registered, repo_module = patched_env
        monkeypatch.setattr(
            repo_module.WebhookRepository,
            "get_by_worksheet_all_orgs",
            lambda self, ws: [
                {"webhook_id": "77", "organization_id": ORG_B, "worksheet_id": ws},
                {"webhook_id": "55", "organization_id": ORG_A, "worksheet_id": ws},
            ],
        )

        ok = asyncio.run(
            _register_webhook_for_config(
                worksheet_id=500,
                org_id=ORG_A,
                config_name="cfg",
                owner_user_id="user-a",
            )
        )

        assert ok is True
        # Only the OTHER org's webhook is protected; ours (55) stays
        # eligible for the pre-cleanup replacement.
        assert registered["protected_ids"] == {77}

    def test_ownership_lookup_failure_fails_closed(self, patched_env, monkeypatch):
        """DB error while loading cross-org rows → do NOT register (and thus
        do not run the destructive pre-cleanup blind)."""
        from src.api.routes.configs import _register_webhook_for_config

        registered, repo_module = patched_env

        def _boom(self, ws):
            raise RuntimeError("supabase down")

        monkeypatch.setattr(
            repo_module.WebhookRepository, "get_by_worksheet_all_orgs", _boom
        )

        ok = asyncio.run(
            _register_webhook_for_config(
                worksheet_id=500,
                org_id=ORG_A,
                config_name="cfg",
                owner_user_id="user-a",
            )
        )

        assert ok is False
        assert registered == {}, "register_webhook ran despite failed ownership read"

    def test_no_foreign_rows_registers_with_empty_protection(
        self, patched_env, monkeypatch
    ):
        from src.api.routes.configs import _register_webhook_for_config

        registered, repo_module = patched_env
        monkeypatch.setattr(
            repo_module.WebhookRepository,
            "get_by_worksheet_all_orgs",
            lambda self, ws: [],
        )

        ok = asyncio.run(
            _register_webhook_for_config(
                worksheet_id=500,
                org_id=ORG_A,
                config_name="cfg",
                owner_user_id="user-a",
            )
        )

        assert ok is True
        assert registered["protected_ids"] == set()


# ---------------------------------------------------------------------------
# WebhookRepository.get_by_worksheet_all_orgs
# ---------------------------------------------------------------------------


class TestGetByWorksheetAllOrgs:
    def test_returns_raw_rows_across_orgs(self):
        from src.services.supabase.webhook_repo import WebhookRepository

        rows = [
            {"webhook_id": "1", "organization_id": ORG_A, "worksheet_id": 500},
            {"webhook_id": "2", "organization_id": ORG_B, "worksheet_id": 500},
        ]
        db = MagicMock()
        db.execute_with_retry.side_effect = lambda fn, _desc="": MagicMock(data=rows)
        repo = WebhookRepository(supabase=db)

        assert repo.get_by_worksheet_all_orgs(500) == rows

    def test_raises_on_db_failure_so_callers_fail_closed(self):
        from src.services.supabase.webhook_repo import WebhookRepository

        db = MagicMock()
        db.execute_with_retry.side_effect = RuntimeError("supabase down")
        repo = WebhookRepository(supabase=db)

        with pytest.raises(RuntimeError):
            repo.get_by_worksheet_all_orgs(500)
