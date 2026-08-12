"""SH-210 — gateway-owned register / renew / zombie lifecycle."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from smarthub_core.crypto import CryptoService
from smarthub_core.platforms.smartsheet import SmartsheetWebhook

from gateway.lifecycle.registrar import (
    I2C_SPEC,
    SMARTQR_SPEC,
    WebhookRegistrar,
    specs_for,
)
from gateway.secrets.store import InMemoryWebhookSecretStore

BASE_URL = "https://webhooks.example.com"
ORG = "org-1"
OTHER_ORG = "org-2"
I2C_CALLBACK = f"{BASE_URL}/webhook/smartsheet/smart-image-to-cell/{ORG}"


def _now_iso(minutes_ago: float = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat().replace("+00:00", "Z")


def _webhook(
    webhook_id: int,
    sheet_id: int,
    *,
    callback_url: str = I2C_CALLBACK,
    status: str = "ENABLED",
    enabled: bool = True,
    secret: str = "s3cr3t",
    created_minutes_ago: float = 120,
) -> SmartsheetWebhook:
    return SmartsheetWebhook(
        id=webhook_id,
        name=f"wh-{webhook_id}",
        scope_object_id=sheet_id,
        callback_url=callback_url,
        shared_secret=secret,
        enabled=enabled,
        status=status,
        created_at=_now_iso(created_minutes_ago),
    )


class FakeSupabase:
    """Minimal Supabase double with select/insert/update/delete per table."""

    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = tables
        self._name: Optional[str] = None
        self._op = "select"
        self._filters: Dict[str, Any] = {}
        self._in: Optional[tuple] = None
        self._payload: Optional[Dict[str, Any]] = None
        self.fail_tables: set = set()

    def table(self, name: str):
        self._name = name
        self._op = "select"
        self._filters = {}
        self._in = None
        self._payload = None
        self.tables.setdefault(name, [])
        return self

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def in_(self, key, values):
        self._in = (key, list(values))
        return self

    def _matches(self, row) -> bool:
        for key, value in self._filters.items():
            if str(row.get(key)) != str(value):
                return False
        if self._in and row.get(self._in[0]) not in self._in[1]:
            return False
        return True

    def execute(self):
        assert self._name is not None
        if self._name in self.fail_tables:
            raise RuntimeError(f"supabase down for {self._name}")
        rows = self.tables[self._name]
        if self._op == "select":
            return SimpleNamespace(data=[r for r in rows if self._matches(r)])
        if self._op == "insert":
            payload = dict(self._payload or {})
            payload.setdefault("id", f"row-{len(rows) + 1}")
            rows.append(payload)
            return SimpleNamespace(data=[payload])
        if self._op == "update":
            hits = [r for r in rows if self._matches(r)]
            for row in hits:
                row.update(self._payload or {})
            return SimpleNamespace(data=hits)
        if self._op == "delete":
            hits = [r for r in rows if self._matches(r)]
            self.tables[self._name] = [r for r in rows if r not in hits]
            return SimpleNamespace(data=hits)
        raise AssertionError(self._op)

    def execute_with_retry(self, operation, description="operation", **_kwargs):
        return operation()


class FakeWebhookService:
    def __init__(self, webhooks: List[SmartsheetWebhook]):
        self.webhooks = list(webhooks)
        self.deleted: List[int] = []
        self.enabled: List[int] = []
        self.url_updates: List[tuple] = []
        self.registered: List[dict] = []
        self._next_id = 900

    async def list_webhooks(self, max_age_s: float = 0):
        return list(self.webhooks)

    async def delete_webhook(self, webhook_id: int) -> bool:
        self.deleted.append(int(webhook_id))
        self.webhooks = [w for w in self.webhooks if w.id != int(webhook_id)]
        return True

    async def enable_webhook(self, webhook_id: int) -> bool:
        self.enabled.append(int(webhook_id))
        return True

    async def update_callback_url(self, webhook_id: int, new_url: str) -> bool:
        self.url_updates.append((int(webhook_id), new_url))
        return True

    async def register_webhook(self, **kwargs) -> SmartsheetWebhook:
        self.registered.append(kwargs)
        self._next_id += 1
        webhook = _webhook(
            self._next_id,
            int(kwargs["worksheet_id"]),
            callback_url=kwargs["callback_url"],
            secret=f"secret-{self._next_id}",
            created_minutes_ago=0,
        )
        self.webhooks.append(webhook)
        return webhook


@pytest.fixture
def crypto():
    return CryptoService(base64.b64encode(b"0" * 32).decode())


def _registrar(supabase, service, store, crypto, **kwargs) -> WebhookRegistrar:
    async def factory(org_id, tool_slug, runner_user_id):
        return service

    return WebhookRegistrar(
        supabase=supabase,
        secret_store=store,
        crypto=crypto,
        webhook_service_factory=factory,
        callback_base_url=BASE_URL,
        **kwargs,
    )


def _i2c_config(sheet_id: int, name: str = "cfg") -> Dict[str, Any]:
    return {
        "id": f"cfg-{sheet_id}",
        "organization_id": ORG,
        "worksheet_id": sheet_id,
        "configuration_name": name,
        "paused": False,
        "processing_mode": "webhook",
    }


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


def test_specs_resolve_known_tools_only_and_exclude_ch2():
    specs = specs_for(["smart-image-to-cell", "smart-qr", "cell-history-2"])
    assert [s.tool_slug for s in specs] == ["smart-image-to-cell", "smart-qr"]
    assert SMARTQR_SPEC.secret_column == "shared_secret_encrypted"
    assert I2C_SPEC.processing_modes == ("webhook", "webhook_v3")


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registers_missing_webhook_and_caches_secret(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService([])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.registered == 1
    assert service.registered[0]["callback_url"] == I2C_CALLBACK
    row = sb.tables["imagetocell_webhooks"][0]
    assert row["worksheet_id"] == 1001
    assert row["status"] == "active"
    # Staging/prod imagetocell_webhooks dialect: webhook_url, no runner_user_id.
    assert row["webhook_url"] == I2C_CALLBACK
    assert "runner_user_id" not in row
    assert "callback_url" not in row
    # Ciphertext in Postgres, plaintext only in the Redis cache.
    assert row["shared_secret"] != "secret-901"
    assert crypto.decrypt(row["shared_secret"]) == "secret-901"
    assert await store.get_secrets(ORG, "smart-image-to-cell") == {"901": "secret-901"}


@pytest.mark.asyncio
async def test_register_keeps_previously_cached_secrets(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    store = InMemoryWebhookSecretStore()
    await store.put_secrets(ORG, "smart-image-to-cell", {"555": "old-secret"})
    registrar = _registrar(sb, FakeWebhookService([]), store, crypto)

    await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    secrets = await store.get_secrets(ORG, "smart-image-to-cell")
    assert secrets["555"] == "old-secret"
    assert secrets["901"] == "secret-901"


# ---------------------------------------------------------------------------
# Renew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_webhook_is_left_alone(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService([_webhook(11, 1001)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert (result.healthy, result.registered) == (1, 0)
    assert service.deleted == []
    assert sb.tables["imagetocell_webhooks"][0]["webhook_id"] == 11


@pytest.mark.asyncio
async def test_disabled_webhook_is_re_enabled(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService(
        [_webhook(11, 1001, status="DISABLED_CALLBACK_FAILED", enabled=False)]
    )
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.re_enabled == 1
    assert service.enabled == [11]
    assert service.deleted == []


@pytest.mark.asyncio
async def test_definitive_failure_is_recreated(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService(
        [_webhook(11, 1001, status="DISABLED_VERIFICATION_FAILED", enabled=False)]
    )
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.registered == 1
    assert service.deleted == [11]


@pytest.mark.asyncio
async def test_new_not_verified_young_webhook_waits(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService(
        [
            _webhook(
                11,
                1001,
                status="NEW_NOT_VERIFIED",
                enabled=False,
                created_minutes_ago=2,
            )
        ]
    )
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.waiting == 1
    assert service.deleted == []


@pytest.mark.asyncio
async def test_new_not_verified_stale_webhook_is_recreated(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService(
        [
            _webhook(
                11,
                1001,
                status="NEW_NOT_VERIFIED",
                enabled=False,
                created_minutes_ago=45,
            )
        ]
    )
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.registered == 1
    assert service.deleted == [11]


@pytest.mark.asyncio
async def test_legacy_callback_url_is_migrated_to_gateway(crypto):
    legacy = f"{BASE_URL}/webhook/smartsheet/smart-image-to-cell/{ORG}/legacy"
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService([_webhook(11, 1001, callback_url=legacy)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.url_updated == 1
    assert service.url_updates == [(11, I2C_CALLBACK)]


# ---------------------------------------------------------------------------
# Ownership + zombies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tools_webhooks_are_never_touched(crypto):
    foreign = f"{BASE_URL}/webhook/smartsheet/cell-history-2/{ORG}"
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService([_webhook(77, 1001, callback_url=foreign)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    # The CH-2 webhook is invisible: I2C registers its own and deletes nothing.
    assert result.registered == 1
    assert service.deleted == []
    assert any(w.id == 77 for w in service.webhooks)


@pytest.mark.asyncio
async def test_webhook_owned_by_other_org_is_protected(crypto):
    sb = FakeSupabase(
        {
            "imagetocell_webhooks": [
                {
                    "id": "row-x",
                    "organization_id": OTHER_ORG,
                    "worksheet_id": 1001,
                    "webhook_id": 42,
                }
            ]
        }
    )
    service = FakeWebhookService([_webhook(42, 1001)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.unowned_skipped == 1
    assert result.registered == 1
    assert 42 not in service.deleted


@pytest.mark.asyncio
async def test_zombie_webhook_without_config_is_deleted(crypto):
    sb = FakeSupabase(
        {
            "imagetocell_webhooks": [
                {
                    "id": "row-z",
                    "organization_id": ORG,
                    "worksheet_id": 2002,
                    "webhook_id": 22,
                }
            ]
        }
    )
    service = FakeWebhookService([_webhook(11, 1001), _webhook(22, 2002)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.zombies_deleted == 1
    assert service.deleted == [22]
    assert all(r.get("webhook_id") != 22 for r in sb.tables["imagetocell_webhooks"])


@pytest.mark.asyncio
async def test_ownership_lookup_failure_blocks_all_deletes(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    sb.fail_tables.add("imagetocell_webhooks")
    service = FakeWebhookService([_webhook(22, 2002)])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.zombies_deleted == 0
    assert service.deleted == []
    # Registration still happens, but without the destructive pre-cleanup.
    assert service.registered[0]["cleanup_existing"] is False


@pytest.mark.asyncio
async def test_duplicates_on_same_sheet_are_collapsed(crypto):
    sb = FakeSupabase({"imagetocell_webhooks": []})
    service = FakeWebhookService(
        [
            _webhook(11, 1001, status="NEW_NOT_VERIFIED", enabled=False),
            _webhook(12, 1001),
        ]
    )
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_org(I2C_SPEC, ORG, [_i2c_config(1001)])

    assert result.healthy == 1
    assert service.deleted == [11]


# ---------------------------------------------------------------------------
# Multi-org / multi-tool sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_all_reads_active_configs_only(crypto):
    sb = FakeSupabase(
        {
            "imagetocell_config": [
                _i2c_config(1001),
                {**_i2c_config(1002), "paused": True},
                {**_i2c_config(1003), "processing_mode": "polling"},
                {**_i2c_config(1004), "connection_status": "needs_connection"},
            ],
            "imagetocell_webhooks": [],
        }
    )
    service = FakeWebhookService([])
    registrar = _registrar(sb, service, InMemoryWebhookSecretStore(), crypto)

    result = await registrar.reconcile_all([I2C_SPEC])

    assert result.configs_checked == 1
    assert result.registered == 1
    assert result.tools == ["smart-image-to-cell"]


@pytest.mark.asyncio
async def test_smartqr_spec_uses_its_own_tables_and_columns(crypto):
    sb = FakeSupabase(
        {
            "smartqr_configs": [
                {
                    "id": "qr-1",
                    "organization_id": ORG,
                    "worksheet_id": 3003,
                    "name": "qr cfg",
                    "pause": False,
                    "runner_user_id": "user-9",
                }
            ],
            "smartqr_webhooks": [],
        }
    )
    service = FakeWebhookService([])
    store = InMemoryWebhookSecretStore()
    registrar = _registrar(sb, service, store, crypto)

    result = await registrar.reconcile_all([SMARTQR_SPEC])

    assert result.registered == 1
    row = sb.tables["smartqr_webhooks"][0]
    assert crypto.decrypt(row["shared_secret_encrypted"]) == "secret-901"
    assert row["runner_user_id"] == "user-9"
    assert (
        service.registered[0]["callback_url"]
        == f"{BASE_URL}/webhook/smartsheet/smart-qr/{ORG}"
    )
    assert await store.get_secrets(ORG, "smart-qr") == {"901": "secret-901"}


@pytest.mark.asyncio
async def test_smartsheet_failure_on_one_org_does_not_abort_cycle(crypto):
    sb = FakeSupabase(
        {
            "imagetocell_config": [
                _i2c_config(1001),
                {**_i2c_config(2002), "organization_id": OTHER_ORG},
            ],
            "imagetocell_webhooks": [],
        }
    )
    good = FakeWebhookService([])

    class Boom(FakeWebhookService):
        async def list_webhooks(self, max_age_s: float = 0):
            raise RuntimeError("smartsheet 500")

    async def factory(org_id, tool_slug, runner_user_id):
        return good if org_id == ORG else Boom([])

    registrar = WebhookRegistrar(
        supabase=sb,
        secret_store=InMemoryWebhookSecretStore(),
        crypto=crypto,
        webhook_service_factory=factory,
        callback_base_url=BASE_URL,
    )

    result = await registrar.reconcile_all([I2C_SPEC])

    assert result.registered == 1
    assert result.errors == 1


# ---------------------------------------------------------------------------
# Manager wiring — the registrar must stay off unless explicitly configured
# ---------------------------------------------------------------------------


def test_registrar_disabled_by_default():
    from gateway.config.settings import Settings
    from gateway.lifecycle.manager import build_registrar

    settings = Settings(_env_file=None)
    assert settings.run_webhook_registrar is False
    registrar, specs = build_registrar(
        settings=settings,
        secret_store=InMemoryWebhookSecretStore(),
        redis_client=object(),
    )
    assert (registrar, specs) == (None, [])


def test_registrar_disabled_without_callback_base_url():
    from gateway.config.settings import Settings
    from gateway.lifecycle.manager import build_registrar

    settings = Settings(
        _env_file=None,
        run_webhook_registrar=True,
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="key",
        app_crypto_key=base64.b64encode(b"0" * 32).decode(),
    )
    registrar, specs = build_registrar(
        settings=settings,
        secret_store=InMemoryWebhookSecretStore(),
        redis_client=object(),
    )
    assert (registrar, specs) == (None, [])


@pytest.mark.asyncio
async def test_manager_cycle_reports_registrar_skipped_when_disabled():
    from gateway.lifecycle.manager import LifecycleManager

    class FakeSync:
        async def refresh_all(self):
            return SimpleNamespace(as_dict=lambda: {"secrets_cached": 0})

    class FakeRedis:
        async def acquire_lock(self, *_a, **_k):
            return "token"

        async def release_lock(self, *_a, **_k):
            return True

        async def extend_lock(self, *_a, **_k):
            return True

    manager = LifecycleManager(
        redis_client=FakeRedis(),
        secret_store=InMemoryWebhookSecretStore(),
        secret_sync=FakeSync(),
    )
    summary = await manager.run_once()
    assert summary["registrar"] == {"skipped": "disabled"}
