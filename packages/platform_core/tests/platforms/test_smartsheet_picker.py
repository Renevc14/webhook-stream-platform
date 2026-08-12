"""Unit tests for SmartsheetPicker.

Picker logic is thin (cache-aside + SDK invocation). The interesting paths:
- cache miss → SDK call → cache write
- cache hit → no SDK call
- corrupt cache → drop + refetch
- force_refresh bypasses cache
- invalidate_workspaces drops the key
- list_workspace_sheets is uncached
"""

import json
from unittest.mock import MagicMock

import pytest

from smarthub_core.db.redis import RedisClient
from smarthub_core.platforms.smartsheet.client import SmartsheetClient
from smarthub_core.platforms.smartsheet.picker import (
    SheetSummary,
    SmartsheetPicker,
    WorkspaceSummary,
)


TEST_ORG = "00000000-0000-0000-0000-000000000077"
TEST_SLUG = "smart-image-to-cell"


def _mock_workspace(id_: int, name: str, permalink: str = ""):
    ws = MagicMock()
    ws.id = id_
    ws.name = name
    ws.permalink = permalink or f"https://app.smartsheet.com/ws/{id_}"
    return ws


def _mock_list_response(workspaces):
    resp = MagicMock()
    resp.data = workspaces
    return resp


@pytest.fixture
def client_mock():
    """A SmartsheetClient mock with the attributes/methods the picker uses."""
    c = MagicMock(spec=SmartsheetClient)
    c.org_id = TEST_ORG
    c.product_slug = TEST_SLUG
    return c


@pytest.fixture
async def picker(rc: RedisClient, client_mock):
    p = SmartsheetPicker(client=client_mock, redis=rc)
    await rc.cache_del(p.cache_key_workspaces)
    return p


class TestListWorkspaces:
    async def test_cache_miss_calls_sdk_and_caches(self, picker, client_mock, rc):
        ws_objects = [
            _mock_workspace(1, "Alpha"),
            _mock_workspace(2, "Beta"),
        ]

        async def fake_call(operation, *, description=""):
            return _mock_list_response(ws_objects)

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspaces()
        assert len(result) == 2
        assert result[0] == WorkspaceSummary(
            id=1, name="Alpha", permalink="https://app.smartsheet.com/ws/1"
        )

        cached = await rc.cache_get(picker.cache_key_workspaces)
        items = json.loads(cached)
        assert items[0]["id"] == 1
        assert items[1]["name"] == "Beta"

    async def test_cache_hit_skips_sdk(self, picker, client_mock, rc):
        # Pre-populate cache
        cached_payload = json.dumps(
            [
                {"id": 99, "name": "Cached", "permalink": "https://x"},
            ]
        )
        await rc.cache_set(picker.cache_key_workspaces, cached_payload, ttl_s=300)

        call_count = {"n": 0}

        async def fake_call(operation, *, description=""):
            call_count["n"] += 1
            return _mock_list_response([])

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspaces()
        assert result == [
            WorkspaceSummary(id=99, name="Cached", permalink="https://x")
        ]
        assert call_count["n"] == 0

    async def test_force_refresh_bypasses_cache(self, picker, client_mock, rc):
        cached_payload = json.dumps([{"id": 99, "name": "Stale", "permalink": ""}])
        await rc.cache_set(picker.cache_key_workspaces, cached_payload, ttl_s=300)

        call_count = {"n": 0}

        async def fake_call(operation, *, description=""):
            call_count["n"] += 1
            return _mock_list_response([_mock_workspace(1, "Fresh")])

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspaces(force_refresh=True)
        assert result[0].name == "Fresh"
        assert call_count["n"] == 1

    async def test_corrupt_cache_dropped_and_refetched(self, picker, client_mock, rc):
        await rc.cache_set(
            picker.cache_key_workspaces, "not-json", ttl_s=60
        )

        async def fake_call(operation, *, description=""):
            return _mock_list_response([_mock_workspace(1, "Recovered")])

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspaces()
        assert result[0].name == "Recovered"

        cached = await rc.cache_get(picker.cache_key_workspaces)
        json.loads(cached)  # raises if still corrupt

    async def test_invalidate_drops_cache(self, picker, rc):
        await rc.cache_set(picker.cache_key_workspaces, "[]", ttl_s=60)
        assert await rc.cache_get(picker.cache_key_workspaces) is not None

        await picker.invalidate_workspaces()
        assert await rc.cache_get(picker.cache_key_workspaces) is None


class TestListWorkspaceSheets:
    async def test_returns_sheets_uncached(self, picker, client_mock):
        # MagicMock(name=...) sets the *mock identifier*, not an attribute,
        # so build sheets explicitly and assign each attribute.
        sheet_a = MagicMock()
        sheet_a.id = 10
        sheet_a.name = "Sheet A"
        sheet_a.permalink = "https://app.smartsheet.com/sheets/10"

        sheet_b = MagicMock()
        sheet_b.id = 11
        sheet_b.name = "Sheet B"
        sheet_b.permalink = ""

        ws = MagicMock()
        ws.sheets = [sheet_a, sheet_b]

        async def fake_call(operation, *, description=""):
            return ws

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspace_sheets(42)
        assert len(result) == 2
        assert result[0] == SheetSummary(
            id=10,
            name="Sheet A",
            permalink="https://app.smartsheet.com/sheets/10",
            workspace_id=42,
        )
        assert result[1].workspace_id == 42

    async def test_no_sheets_returns_empty_list(self, picker, client_mock):
        ws = MagicMock()
        ws.sheets = None

        async def fake_call(operation, *, description=""):
            return ws

        client_mock.call_with_retry_async = fake_call

        result = await picker.list_workspace_sheets(42)
        assert result == []
