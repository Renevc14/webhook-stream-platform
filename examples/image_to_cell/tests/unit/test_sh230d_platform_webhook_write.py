"""SH-230 D — I2C WebhookRepository writes platform_webhook_registrations first."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from smarthub_core.crypto import CryptoError
from smarthub_core.webhooks.platform_registrations import PLATFORM_WEBHOOKS_TABLE


def _fake_encrypt(plaintext: str) -> str:
    return f"ENC::{plaintext}"


def _fake_decrypt(value: str) -> str:
    if value.startswith("ENC::"):
        return value[len("ENC::") :]
    raise CryptoError("not encrypted")


class _Result:
    def __init__(self, data: Optional[list] = None):
        self.data = data or []


class MultiTableFake:
    """Supabase double that tracks platform + tool table writes."""

    def __init__(self, tables: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self.tables: Dict[str, List[Dict[str, Any]]] = tables or {}
        self.fail_tables: set[str] = set()
        self._id = 0

    def table(self, name: str) -> "_Q":
        return _Q(self, name)

    def execute_with_retry(self, operation, description: str = "operation", **kwargs):
        return operation()


class _Q:
    def __init__(self, sb: MultiTableFake, table: str):
        self._sb = sb
        self._table = table
        self._filters: Dict[str, Any] = {}
        self._payload: Optional[dict] = None
        self._op = "select"
        self._on_conflict: Optional[str] = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def eq(self, k, v):
        self._filters[k] = v
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = dict(payload)
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        if self._table in self._sb.fail_tables:
            raise RuntimeError(f"forced fail {self._table}")
        rows = self._sb.tables.setdefault(self._table, [])
        if self._op == "select":
            matched = [
                r for r in rows if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
            ]
            return _Result(matched)
        if self._op == "insert":
            assert self._payload is not None
            row = dict(self._payload)
            self._sb._id += 1
            row.setdefault("id", f"id-{self._sb._id}")
            rows.append(row)
            return _Result([row])
        if self._op == "update":
            assert self._payload is not None
            updated = []
            for r in rows:
                if all(str(r.get(k)) == str(v) for k, v in self._filters.items()):
                    r.update(self._payload)
                    updated.append(r)
            return _Result(updated)
        if self._op == "upsert":
            assert self._payload is not None
            key = self._on_conflict or "worksheet_id"
            for r in rows:
                if str(r.get(key)) == str(self._payload.get(key)):
                    r.update(self._payload)
                    return _Result([r])
            row = dict(self._payload)
            self._sb._id += 1
            row.setdefault("id", f"id-{self._sb._id}")
            rows.append(row)
            return _Result([row])
        if self._op == "delete":
            keep = []
            deleted = []
            for r in rows:
                if all(str(r.get(k)) == str(v) for k, v in self._filters.items()):
                    deleted.append(r)
                else:
                    keep.append(r)
            self._sb.tables[self._table] = keep
            return _Result(deleted)
        raise AssertionError(self._op)


@pytest.fixture
def patched_crypto(monkeypatch):
    from src.services.supabase import webhook_repo as repo_module

    monkeypatch.setattr(repo_module, "encrypt_token", _fake_encrypt)
    monkeypatch.setattr(repo_module, "decrypt_token", _fake_decrypt)
    return repo_module


def test_save_writes_platform_then_tool_mirror(patched_crypto):
    from src.models.webhook import WebhookRegistration

    db = MultiTableFake()
    repo = patched_crypto.WebhookRepository(supabase=db)

    repo.save(
        WebhookRegistration(
            webhook_id=123,
            worksheet_id=999,
            organization_id="org-1",
            shared_secret="hunter2",
            webhook_url="https://webhooks.example/cb",
            status="active",
        )
    )

    platform = db.tables[PLATFORM_WEBHOOKS_TABLE]
    tool = db.tables["imagetocell_webhooks"]
    assert len(platform) == 1
    assert platform[0]["tool_slug"] == "smart-image-to-cell"
    assert platform[0]["provider"] == "smartsheet"
    assert platform[0]["webhook_id"] == 123
    assert platform[0]["shared_secret"] == "ENC::hunter2"
    assert platform[0]["callback_url"] == "https://webhooks.example/cb"
    assert len(tool) == 1
    assert tool[0]["shared_secret"] == "ENC::hunter2"


def test_save_requires_platform_even_if_tool_mirror_fails(patched_crypto):
    from src.models.webhook import WebhookRegistration
    from smarthub_core.webhooks.platform_registrations import PlatformRegistrationError

    db = MultiTableFake()
    db.fail_tables.add(PLATFORM_WEBHOOKS_TABLE)
    repo = patched_crypto.WebhookRepository(supabase=db)

    with pytest.raises(PlatformRegistrationError):
        repo.save(
            WebhookRegistration(
                webhook_id=1,
                worksheet_id=2,
                organization_id="org-1",
                shared_secret="x",
            )
        )
    assert db.tables.get("imagetocell_webhooks", []) == []


def test_save_tool_mirror_soft_fail_keeps_platform(patched_crypto):
    from src.models.webhook import WebhookRegistration

    db = MultiTableFake()
    db.fail_tables.add("imagetocell_webhooks")
    repo = patched_crypto.WebhookRepository(supabase=db)

    repo.save(
        WebhookRegistration(
            webhook_id=1,
            worksheet_id=2,
            organization_id="org-1",
            shared_secret="x",
        )
    )
    assert len(db.tables[PLATFORM_WEBHOOKS_TABLE]) == 1
    assert db.tables.get("imagetocell_webhooks", []) == []


def test_update_secret_updates_platform(patched_crypto):
    db = MultiTableFake(
        {
            "imagetocell_webhooks": [
                {
                    "webhook_id": "55",
                    "worksheet_id": 999,
                    "organization_id": "org-1",
                    "shared_secret": "ENC::old",
                    "status": "active",
                    "webhook_url": "https://cb",
                }
            ]
        }
    )
    repo = patched_crypto.WebhookRepository(supabase=db)
    repo.update_secret(worksheet_id=999, secret="new-plain")

    platform = db.tables[PLATFORM_WEBHOOKS_TABLE]
    assert len(platform) == 1
    assert platform[0]["shared_secret"] == "ENC::new-plain"
    assert platform[0]["webhook_id"] == 55
    tool = db.tables["imagetocell_webhooks"][0]
    assert tool["shared_secret"] == "ENC::new-plain"
