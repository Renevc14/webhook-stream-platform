"""SH-230 D — platform_webhook_registrations is write SoT."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from smarthub_core.webhooks.platform_registrations import (
    PLATFORM_UPSERT_SQL,
    PLATFORM_WEBHOOKS_TABLE,
    PlatformRegistrationError,
    delete_platform_registration,
    delete_platform_registration_by_worksheet,
    upsert_platform_registration,
)


class _Result:
    def __init__(self, data: Optional[list] = None):
        self.data = data or []


class FakeSupabase:
    def __init__(self, tables: Optional[dict[str, list]] = None):
        self.tables: dict[str, list] = tables or {}
        self.fail_tables: set[str] = set()
        self._id = 0

    def table(self, name: str) -> "_Query":
        return _Query(self, name)

    def execute_with_retry(self, operation, description: str = "operation", **kwargs):
        return operation()


class _Query:
    def __init__(self, sb: FakeSupabase, table: str):
        self._sb = sb
        self._table = table
        self._filters: dict[str, Any] = {}
        self._payload: Optional[dict] = None
        self._op = "select"
        self._select = "*"

    def select(self, cols: str = "*"):
        self._op = "select"
        self._select = cols
        return self

    def eq(self, k: str, v: Any):
        self._filters[k] = v
        return self

    def insert(self, payload: dict):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        if self._table in self._sb.fail_tables:
            raise RuntimeError(f"forced failure on {self._table}")
        rows = self._sb.tables.setdefault(self._table, [])
        if self._op == "select":
            matched = [
                r
                for r in rows
                if all(r.get(k) == v for k, v in self._filters.items())
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
                if all(r.get(k) == v for k, v in self._filters.items()):
                    r.update(self._payload)
                    updated.append(r)
            return _Result(updated)
        if self._op == "delete":
            keep = []
            deleted = []
            for r in rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    deleted.append(r)
                else:
                    keep.append(r)
            self._sb.tables[self._table] = keep
            return _Result(deleted)
        raise AssertionError(self._op)


def test_upsert_insert_then_update():
    sb = FakeSupabase()
    assert upsert_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        organization_id="org-1",
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
        organization_id="org-1",
        worksheet_id=11,
        webhook_id=102,
        shared_secret_ciphertext="cipher-2",
        status="active",
    )
    assert len(sb.tables[PLATFORM_WEBHOOKS_TABLE]) == 1
    assert rows[0]["webhook_id"] == 102
    assert rows[0]["shared_secret"] == "cipher-2"


def test_upsert_required_raises():
    sb = FakeSupabase()
    sb.fail_tables.add(PLATFORM_WEBHOOKS_TABLE)
    with pytest.raises(PlatformRegistrationError):
        upsert_platform_registration(
            sb,
            provider="smartsheet",
            tool_slug="smart-image-to-cell",
            organization_id="org-1",
            worksheet_id=11,
            webhook_id=101,
        )


def test_upsert_soft_fail_returns_false():
    sb = FakeSupabase()
    sb.fail_tables.add(PLATFORM_WEBHOOKS_TABLE)
    assert not upsert_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        organization_id="org-1",
        worksheet_id=11,
        webhook_id=101,
        soft_fail=True,
    )


def test_delete_by_webhook_and_worksheet():
    sb = FakeSupabase(
        {
            PLATFORM_WEBHOOKS_TABLE: [
                {
                    "id": "p1",
                    "provider": "smartsheet",
                    "tool_slug": "smart-image-to-cell",
                    "organization_id": "org-1",
                    "worksheet_id": 11,
                    "webhook_id": 101,
                    "status": "active",
                },
                {
                    "id": "p2",
                    "provider": "smartsheet",
                    "tool_slug": "smart-qr",
                    "organization_id": "org-1",
                    "worksheet_id": 22,
                    "webhook_id": 202,
                    "status": "active",
                },
            ]
        }
    )
    assert delete_platform_registration(
        sb,
        provider="smartsheet",
        tool_slug="smart-image-to-cell",
        webhook_id=101,
    )
    assert all(r["id"] != "p1" for r in sb.tables[PLATFORM_WEBHOOKS_TABLE])

    assert delete_platform_registration_by_worksheet(
        sb,
        provider="smartsheet",
        tool_slug="smart-qr",
        organization_id="org-1",
        worksheet_id=22,
    )
    assert sb.tables[PLATFORM_WEBHOOKS_TABLE] == []


def test_platform_upsert_sql_mentions_identity_conflict():
    assert "ON CONFLICT (provider, tool_slug, organization_id, worksheet_id)" in (
        PLATFORM_UPSERT_SQL
    )
