"""Unit tests for SmartsheetWebhookService (SH-207 / SH-193)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from smarthub_core.platforms.smartsheet.webhook_service import (
    SmartsheetWebhook,
    SmartsheetWebhookService,
    WebhookStatus,
)


def _wh(
    *,
    webhook_id: int,
    sheet_id: int,
    callback: str = "https://gateway.example/webhook/smartsheet/smart-image-to-cell/org-a",
    secret: str = "",
    enabled: bool = True,
    status: str = WebhookStatus.ENABLED,
) -> SmartsheetWebhook:
    return SmartsheetWebhook(
        id=webhook_id,
        name=f"wh-{webhook_id}",
        scope_object_id=sheet_id,
        callback_url=callback,
        shared_secret=secret,
        enabled=enabled,
        status=status,
    )


class FakeSdkWebhooks:
    def __init__(
        self,
        *,
        listed: Optional[List[Any]] = None,
        create_result: Optional[Any] = None,
        enable_result: Optional[Any] = None,
    ) -> None:
        self.listed = listed or []
        self.create_result = create_result
        self.enable_result = enable_result
        self.deleted: List[int] = []
        self.updated: List[tuple] = []
        self.created: List[Any] = []

    def list_webhooks(self, page_size: int = 100, page: int = 1):
        start = (page - 1) * page_size
        end = start + page_size
        chunk = self.listed[start:end]
        return SimpleNamespace(data=chunk)

    def get_webhook(self, webhook_id: int):
        for item in self.listed:
            wid = item.id if hasattr(item, "id") else item.get("id")
            if wid == webhook_id:
                return SimpleNamespace(data=item)
        raise Exception("not found 1006")

    def create_webhook(self, spec):
        self.created.append(spec)
        return SimpleNamespace(result=self.create_result)

    def update_webhook(self, webhook_id: int, spec):
        self.updated.append((webhook_id, spec))
        return SimpleNamespace(result=self.enable_result or SimpleNamespace(status="ENABLED"))

    def delete_webhook(self, webhook_id: int):
        self.deleted.append(webhook_id)
        return SimpleNamespace()


class FakeSmartsheetClient:
    """Minimal stand-in: call_with_retry_async runs the lambda against Fake SDK."""

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    async def call_with_retry_async(self, operation, *, description: str = "", **_kw):
        return operation(self._sdk)


def test_from_api_response_object_and_dict():
    obj = SimpleNamespace(
        id=11,
        name="n",
        scope_object_id=99,
        callback_url="https://cb",
        shared_secret="sec",
        enabled=True,
        status="ENABLED",
        created_at=None,
        modified_at=None,
    )
    wh = SmartsheetWebhook.from_api_response(obj)
    assert wh.id == 11
    assert wh.scope_object_id == 99
    assert wh.shared_secret == "sec"
    assert wh.is_healthy

    wh2 = SmartsheetWebhook.from_api_response(
        {
            "id": 12,
            "name": "n2",
            "scopeObjectId": 100,
            "callbackUrl": "https://cb2",
            "sharedSecret": "s2",
            "enabled": False,
            "status": "DISABLED_CALLBACK_FAILED",
        }
    )
    assert wh2.id == 12
    assert wh2.is_disabled


@pytest.mark.asyncio
async def test_list_webhooks_paginates():
    listed = [
        {
            "id": i,
            "name": f"w{i}",
            "scopeObjectId": 1,
            "callbackUrl": "https://cb",
            "sharedSecret": "",
            "enabled": True,
            "status": "ENABLED",
        }
        for i in range(150)
    ]
    sdk = SimpleNamespace(Webhooks=FakeSdkWebhooks(listed=listed))
    svc = SmartsheetWebhookService(FakeSmartsheetClient(sdk))
    result = await svc.list_webhooks()
    assert len(result) == 150
    # Cache hit
    cached = await svc.list_webhooks(max_age_s=30)
    assert cached is result


@pytest.mark.asyncio
async def test_cleanup_respects_protected_ids_and_ownership():
    ours = "https://gateway.example/webhook/smartsheet/smart-image-to-cell/org-a"
    theirs = "https://cell-history.example/webhook/smartsheet"
    listed = [
        _wh(webhook_id=1, sheet_id=500, callback=ours),
        _wh(webhook_id=2, sheet_id=500, callback=ours),  # protected
        _wh(webhook_id=3, sheet_id=500, callback=theirs),
        _wh(webhook_id=4, sheet_id=999, callback=ours),  # other sheet
    ]
    sdk = SimpleNamespace(Webhooks=FakeSdkWebhooks(listed=listed))
    svc = SmartsheetWebhookService(FakeSmartsheetClient(sdk))

    deleted = await svc.cleanup_all_webhooks_for_worksheet(
        500,
        protected_ids={2},
        ownership_patterns=[ours],
    )
    assert deleted == 1
    assert sdk.Webhooks.deleted == [1]


@pytest.mark.asyncio
async def test_register_captures_secret_and_skips_foreign_duplicates():
    ours = "https://gateway.example/webhook/smartsheet/smart-image-to-cell/org-a"
    foreign = "https://other.example/hook"
    create_result = SimpleNamespace(
        id=77,
        name="new",
        scopeObjectId=500,
        callbackUrl=ours,
        sharedSecret="brand-new-secret",
        enabled=False,
        status="NEW_NOT_VERIFIED",
        to_dict=lambda: {
            "id": 77,
            "sharedSecret": "brand-new-secret",
            "status": "NEW_NOT_VERIFIED",
        },
    )
    # After create, list returns new + a foreign duplicate on same sheet
    listed_after = [
        _wh(webhook_id=77, sheet_id=500, callback=ours, secret="brand-new-secret"),
        _wh(webhook_id=88, sheet_id=500, callback=foreign),
        _wh(webhook_id=99, sheet_id=500, callback=ours),  # our stale duplicate
    ]

    webhooks_api = FakeSdkWebhooks(
        listed=[],
        create_result=create_result,
        enable_result=SimpleNamespace(status="ENABLED", sharedSecret=""),
    )

    class FlipListClient(FakeSmartsheetClient):
        def __init__(self):
            super().__init__(SimpleNamespace(Webhooks=webhooks_api))
            self._calls = 0

        async def call_with_retry_async(self, operation, *, description: str = "", **_kw):
            self._calls += 1
            # After create+enable, seed list for post-cleanup
            if "list_webhooks" in description or description.startswith("list"):
                webhooks_api.listed = listed_after
            result = operation(self._sdk)
            # Also flip after create so get_webhooks_for_worksheet sees them
            if "create_webhook" in description:
                webhooks_api.listed = listed_after
            return result

    svc = SmartsheetWebhookService(FlipListClient())
    # Skip pre-cleanup so we focus on create + post-cleanup
    wh = await svc.register_webhook(
        worksheet_id=500,
        callback_url=ours,
        name="test-wh",
        cleanup_existing=False,
        protected_ids=set(),
        ownership_patterns=[ours],
    )
    assert wh.id == 77
    assert wh.shared_secret == "brand-new-secret"
    # Foreign 88 kept; our stale 99 deleted
    assert 99 in webhooks_api.deleted
    assert 88 not in webhooks_api.deleted


@pytest.mark.asyncio
async def test_get_webhook_not_found_returns_none():
    sdk = SimpleNamespace(Webhooks=FakeSdkWebhooks(listed=[]))
    svc = SmartsheetWebhookService(FakeSmartsheetClient(sdk))
    assert await svc.get_webhook(404) is None


@pytest.mark.asyncio
async def test_enable_disable_delete_update():
    sdk = SimpleNamespace(Webhooks=FakeSdkWebhooks(listed=[]))
    svc = SmartsheetWebhookService(FakeSmartsheetClient(sdk))
    assert await svc.enable_webhook(1) is True
    assert await svc.disable_webhook(1) is True
    assert await svc.update_callback_url(1, "https://new") is True
    assert await svc.delete_webhook(1) is True
    assert len(sdk.Webhooks.updated) == 3
    assert sdk.Webhooks.deleted == [1]
