"""SH-175 — lifecycle / register use per-org gateway callback URLs (via smarthub_core)."""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from smarthub_core.platforms.smartsheet import (
    gateway_callback_url,
    resolve_webhook_callback_url,
)


ORG = "78e2278d-ffaa-4299-a969-9424275cc207"
SLUG = "smart-image-to-cell"
BASE = "https://webhooks.prodactivelabs.com"
LEGACY = "https://smart-image-to-cell.prodactivelabs.com/webhook/smartsheet"
GATEWAY_ORG = f"{BASE}/webhook/smartsheet/{SLUG}/{ORG}"


def test_settings_resolve_uses_gateway_when_base_set():
    assert (
        resolve_webhook_callback_url(
            organization_id=ORG,
            gateway_base_url=BASE,
            legacy_callback_url=LEGACY,
            product_slug=SLUG,
        )
        == GATEWAY_ORG
    )
    assert gateway_callback_url(BASE, SLUG, ORG) == GATEWAY_ORG


@pytest.mark.asyncio
async def test_lifecycle_updates_legacy_url_to_gateway():
    from tests.unit.test_sh178_lifecycle_cross_org_cleanup import (
        FakeWebhookService,
        build_manager,
        make_ss_webhook,
    )

    sheet = 1124052763627396
    wh = make_ss_webhook(id=7320483440945028, sheet=sheet, callback_url=LEGACY)
    service = FakeWebhookService([wh])
    updated: List[str] = []

    async def _update(webhook_id: int, new_url: str) -> bool:
        updated.append(new_url)
        wh.callback_url = new_url
        return True

    service.update_callback_url = _update  # type: ignore[method-assign]

    manager = build_manager(callback_url=LEGACY, gateway_base_url=BASE)
    manager._update_webhook_url_in_db = MagicMock()

    config = {
        "worksheet_id": sheet,
        "organization_id": ORG,
        "configuration_name": "testfree",
    }
    result = await manager._check_webhook_for_config(
        config=config,
        webhook_service=service,
        ss_webhooks=[wh],
        db_webhook={"webhook_id": str(wh.id), "worksheet_id": sheet},
    )
    assert result == "url_updated"
    assert updated == [GATEWAY_ORG]
    manager._update_webhook_url_in_db.assert_called_once_with(wh.id, GATEWAY_ORG)


def test_zombie_scan_treats_gateway_and_legacy_as_ours():
    from tests.unit.test_sh178_lifecycle_cross_org_cleanup import (
        build_manager,
        make_ss_webhook,
    )

    manager = build_manager(callback_url=LEGACY, gateway_base_url=BASE)
    ours_legacy = make_ss_webhook(id=1, sheet=111, callback_url=LEGACY)
    ours_gw = make_ss_webhook(id=2, sheet=222, callback_url=GATEWAY_ORG)
    foreign = make_ss_webhook(
        id=3, sheet=333, callback_url="https://other.example/webhook"
    )
    wrong, _dup, _stale, skipped = manager._find_zombie_webhooks(
        smartsheet_webhooks=[ours_legacy, ours_gw, foreign],
        db_webhooks=[
            {"webhook_id": "1", "worksheet_id": 111},
            {"webhook_id": "2", "worksheet_id": 222},
        ],
        expected_sheets={999},
        callback_url_patterns=manager._settings.webhook_callback_ownership_patterns(),
        org_id=ORG,
        global_webhook_owners={1: ORG, 2: ORG},
        global_active_sheets={999},
    )
    assert {w.id for w in wrong} == {1, 2}
    assert skipped == 0
    assert all(w.id != 3 for w in wrong)
