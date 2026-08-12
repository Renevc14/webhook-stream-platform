"""SH-230 D — SmartQR WebhookRepository upserts platform_webhook_registrations."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple
from uuid import UUID

import pytest

from smarthub_core.webhooks.platform_registrations import PLATFORM_UPSERT_SQL


ORG = UUID("11111111-1111-1111-1111-111111111111")
RUNNER = UUID("22222222-2222-2222-2222-222222222222")


class FakePool:
    def __init__(self):
        self.calls: List[Tuple[str, tuple]] = []
        self.fail_on: Optional[str] = None
        self._select_webhook_id: Optional[int] = 501

    async def execute(self, sql: str, *args: Any):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"forced fail matching {self.fail_on}")
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        if self._select_webhook_id is None:
            return None
        return {"webhook_id": self._select_webhook_id}


@pytest.mark.asyncio
async def test_upsert_writes_platform_sql_first(monkeypatch):
    from smart_qr.adapters.db import repositories as repo_mod

    pool = FakePool()
    monkeypatch.setattr(repo_mod, "get_pool", lambda: pool)

    await repo_mod.WebhookRepository().upsert(
        organization_id=ORG,
        worksheet_id=99,
        webhook_id=501,
        shared_secret_encrypted="cipher",
        runner_user_id=RUNNER,
        callback_url="https://webhooks.example/cb",
    )

    assert pool.calls, "expected SQL calls"
    first_sql, first_args = pool.calls[0]
    assert first_sql.strip() == PLATFORM_UPSERT_SQL.strip()
    assert first_args[0] == "smartsheet"
    assert first_args[1] == "smart-qr"
    assert first_args[2] == ORG
    assert first_args[3] == 99
    assert first_args[4] == 501
    assert first_args[5] == "cipher"
    assert first_args[6] == "https://webhooks.example/cb"
    # Tool mirror delete + insert follow.
    assert any("INSERT INTO smartqr_webhooks" in sql for sql, _ in pool.calls)


@pytest.mark.asyncio
async def test_upsert_tool_mirror_soft_fail_keeps_platform(monkeypatch):
    from smart_qr.adapters.db import repositories as repo_mod

    pool = FakePool()
    pool.fail_on = "smartqr_webhooks"
    monkeypatch.setattr(repo_mod, "get_pool", lambda: pool)

    await repo_mod.WebhookRepository().upsert(
        organization_id=ORG,
        worksheet_id=99,
        webhook_id=501,
        shared_secret_encrypted="cipher",
        runner_user_id=None,
    )

    assert any(
        sql.strip() == PLATFORM_UPSERT_SQL.strip() for sql, _ in pool.calls
    )


@pytest.mark.asyncio
async def test_delete_for_worksheet_deletes_platform(monkeypatch):
    from smart_qr.adapters.db import repositories as repo_mod
    from smarthub_core.webhooks.platform_registrations import (
        PLATFORM_DELETE_BY_WORKSHEET_SQL,
    )

    pool = FakePool()
    monkeypatch.setattr(repo_mod, "get_pool", lambda: pool)

    wid = await repo_mod.WebhookRepository().delete_for_worksheet(ORG, 99)
    assert wid == 501
    assert any(
        sql.strip() == PLATFORM_DELETE_BY_WORKSHEET_SQL.strip()
        for sql, _ in pool.calls
    )
