"""SH-230 Phase B — soft-fail dual-write to platform_webhook_registrations."""

from __future__ import annotations

import base64
import pytest
from smarthub_core.crypto import CryptoService

from gateway.lifecycle.platform_registrations import (
    PLATFORM_WEBHOOKS_TABLE,
    count_platform_vs_tool_parity,
    delete_platform_registration,
    upsert_platform_registration,
)
from gateway.lifecycle.registrar import I2C_SPEC
from gateway.secrets.store import InMemoryWebhookSecretStore

from tests.unit.test_sh210_webhook_registrar import (
    BASE_URL,
    ORG,
    FakeSupabase,
    FakeWebhookService,
    _i2c_config,
    _registrar,
    _webhook,
)


@pytest.fixture
def crypto():
    return CryptoService(base64.b64encode(b"0" * 32).decode())


def test_upsert_platform_registration_insert_then_update():
    sb = FakeSupabase({})
    assert upsert_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        organization_id=ORG,
        worksheet_id=11,
        webhook_id=101,
        shared_secret_ciphertext="cipher-1",
        callback_url="https://example/cb",
        status="active",
        runner_user_id="user-1",
        config_id="cfg-11",
    )
    rows = sb.tables[PLATFORM_WEBHOOKS_TABLE]
    assert len(rows) == 1
    assert rows[0]["webhook_id"] == 101
    assert rows[0]["shared_secret"] == "cipher-1"

    assert upsert_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        organization_id=ORG,
        worksheet_id=11,
        webhook_id=102,
        shared_secret_ciphertext="cipher-2",
        status="active",
    )
    assert len(sb.tables[PLATFORM_WEBHOOKS_TABLE]) == 1
    assert rows[0]["webhook_id"] == 102
    assert rows[0]["shared_secret"] == "cipher-2"


def test_upsert_platform_registration_soft_fails():
    sb = FakeSupabase({})
    sb.fail_tables.add(PLATFORM_WEBHOOKS_TABLE)
    assert not upsert_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        organization_id=ORG,
        worksheet_id=11,
        webhook_id=101,
        soft_fail=True,
    )


def test_delete_platform_registration():
    sb = FakeSupabase(
        {
            PLATFORM_WEBHOOKS_TABLE: [
                {
                    "id": "p1",
                    "provider": "smartsheet",
                    "tool_slug": "smart-image-to-cell",
                    "organization_id": ORG,
                    "worksheet_id": 11,
                    "webhook_id": 101,
                    "status": "active",
                }
            ]
        }
    )
    assert delete_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        webhook_id=101,
    )
    assert sb.tables[PLATFORM_WEBHOOKS_TABLE] == []


def test_parity_counts_matched_and_only_tool():
    sb = FakeSupabase(
        {
            "imagetocell_webhooks": [
                {"webhook_id": 1, "status": "active"},
                {"webhook_id": 2, "status": "active"},
            ],
            PLATFORM_WEBHOOKS_TABLE: [
                {
                    "webhook_id": 1,
                    "status": "active",
                    "provider": "smartsheet",
                    "tool_slug": "smart-image-to-cell",
                }
            ],
        }
    )
    stats = count_platform_vs_tool_parity(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        tool_table="imagetocell_webhooks",
    )
    assert stats == {
        "tool_count": 2,
        "platform_count": 1,
        "only_tool": 1,
        "only_platform": 0,
        "matched": 1,
    }


@pytest.mark.asyncio
async def test_registrar_dual_writes_on_register(crypto):
    sheet_id = 55
    sb = FakeSupabase(
        {
            "imagetocell_configs": [_i2c_config(sheet_id)],
            "imagetocell_webhooks": [],
        }
    )
    service = FakeWebhookService([])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto, platform_dual_write=True)

    await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(sheet_id)])

    tool_rows = sb.tables["imagetocell_webhooks"]
    platform_rows = sb.tables[PLATFORM_WEBHOOKS_TABLE]
    assert len(tool_rows) == 1
    assert len(platform_rows) == 1
    assert platform_rows[0]["tool_slug"] == "smart-image-to-cell"
    assert platform_rows[0]["provider"] == "smartsheet"
    assert platform_rows[0]["worksheet_id"] == sheet_id
    assert platform_rows[0]["webhook_id"] == tool_rows[0]["webhook_id"]
    assert platform_rows[0]["config_id"] == f"cfg-{sheet_id}"
    assert platform_rows[0].get("shared_secret")


@pytest.mark.asyncio
async def test_registrar_tool_mirror_soft_fail_keeps_platform_row(crypto):
    """SH-230 D: platform SoT required; tool-table mirror soft-fails."""
    sheet_id = 66
    sb = FakeSupabase(
        {
            "imagetocell_configs": [_i2c_config(sheet_id)],
            "imagetocell_webhooks": [],
        }
    )
    sb.fail_tables.add("imagetocell_webhooks")
    service = FakeWebhookService([])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto, platform_dual_write=True)

    await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(sheet_id)])

    assert len(sb.tables[PLATFORM_WEBHOOKS_TABLE]) == 1
    assert sb.tables.get("imagetocell_webhooks", []) == []


@pytest.mark.asyncio
async def test_registrar_skips_tool_mirror_when_dual_write_disabled(crypto):
    sheet_id = 77
    sb = FakeSupabase(
        {
            "imagetocell_configs": [_i2c_config(sheet_id)],
            "imagetocell_webhooks": [],
        }
    )
    service = FakeWebhookService([])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto, platform_dual_write=False)

    await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(sheet_id)])

    assert len(sb.tables[PLATFORM_WEBHOOKS_TABLE]) == 1
    assert sb.tables.get("imagetocell_webhooks", []) == []


@pytest.mark.asyncio
async def test_registrar_deletes_platform_mirror_on_zombie(crypto):
    sheet_id = 88
    callback = f"{BASE_URL}/webhook/smartsheet/smart-image-to-cell/{ORG}"
    wh = _webhook(501, sheet_id, callback_url=callback)
    sb = FakeSupabase(
        {
            "imagetocell_configs": [],
            "imagetocell_webhooks": [
                {
                    "id": "row-1",
                    "organization_id": ORG,
                    "worksheet_id": sheet_id,
                    "webhook_id": 501,
                    "status": "active",
                }
            ],
            PLATFORM_WEBHOOKS_TABLE: [
                {
                    "id": "p-1",
                    "provider": "smartsheet",
                    "tool_slug": "smart-image-to-cell",
                    "organization_id": ORG,
                    "worksheet_id": sheet_id,
                    "webhook_id": 501,
                    "status": "active",
                }
            ],
        }
    )
    service = FakeWebhookService([wh])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto, platform_dual_write=True)

    # Keep one active config on a different sheet so ownership lookup works,
    # while sheet 88 becomes a zombie.
    await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert 501 in service.deleted
    assert all(r.get("webhook_id") != 501 for r in sb.tables["imagetocell_webhooks"])
    assert all(r.get("webhook_id") != 501 for r in sb.tables[PLATFORM_WEBHOOKS_TABLE])
