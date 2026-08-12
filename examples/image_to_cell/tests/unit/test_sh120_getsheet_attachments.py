"""SH-120 — get_sheet must not use include=attachments on Get Sheet.

Smartsheet started rejecting `include=attachments` on the Get Sheet endpoint
with HTTP 400 / errorCode 1008 ("Unable to parse request") on 2026-05-29, which
broke backfill (full-sheet scan). Webhooks were unaffected because they use the
per-row endpoint, where the option still works.

The fix fetches rows WITHOUT the include and merges attachment metadata from the
dedicated List Attachments endpoint, grouping by ``parentId``. These tests pin
that behaviour so the regression cannot silently come back.

SH-100: ``SheetService`` is now an async wrapper backed by the smarthub_core
``SmartsheetClient``. The tests inject a fake client whose
``call_with_retry_async`` simply runs the operation against a stand-in SDK
client we control, so the assertions can still observe which Smartsheet
endpoints were touched and with which kwargs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.smartsheet.sheets import SheetService


def _result(payload):
    """Wrap a dict so it mimics the Smartsheet SDK result (.to_dict())."""
    r = MagicMock()
    r.to_dict.return_value = payload
    return r


def _make_client(sheet_payload, attachments):
    """Build a ``SmartsheetClient`` stand-in whose ``call_with_retry_async``
    runs the operation against a controllable fake SDK client."""
    sdk = MagicMock()
    sdk.Sheets.get_sheet.return_value = _result(sheet_payload)
    sdk.Attachments.list_all_attachments.return_value = _result({"data": attachments})

    client = MagicMock(name="SmartsheetClient")

    async def _call(operation, description=None):
        return operation(sdk)

    client.call_with_retry_async = _call
    # Expose the SDK so tests can assert on call args.
    client._fake_sdk = sdk
    return client


@pytest.mark.asyncio
async def test_get_sheet_merges_attachments_by_parent_row():
    sheet_payload = {
        "columns": [{"id": 1}],
        "rows": [{"id": 100}, {"id": 200}, {"id": 300}],
        "totalRowCount": 3,
    }
    attachments = [
        {"id": 11, "name": "a.jpg", "mimeType": "image/jpeg", "parentType": "ROW", "parentId": 100},
        {"id": 12, "name": "b.jpg", "mimeType": "image/jpeg", "parentType": "ROW", "parentId": 100},
        {"id": 13, "name": "c.jpg", "mimeType": "image/jpeg", "parentType": "ROW", "parentId": 200},
        # Non-row attachment (e.g. on a comment) must be ignored
        {"id": 14, "name": "d.jpg", "mimeType": "image/jpeg", "parentType": "COMMENT", "parentId": 999},
    ]
    client = _make_client(sheet_payload, attachments)

    sheet = await SheetService(client).get_sheet(123, include_attachments=True)

    rows = {r["id"]: r for r in sheet["rows"]}
    assert [a["id"] for a in rows[100]["attachments"]] == [11, 12]
    assert [a["id"] for a in rows[200]["attachments"]] == [13]
    # Row with no attachments gets an empty list, not missing key
    assert rows[300]["attachments"] == []

    # Regression guard: Get Sheet must NOT be called with include=attachments
    _, kwargs = client._fake_sdk.Sheets.get_sheet.call_args
    assert "include" not in kwargs
    # Attachments came from the dedicated List Attachments endpoint
    client._fake_sdk.Attachments.list_all_attachments.assert_called_once()


@pytest.mark.asyncio
async def test_get_sheet_skips_attachment_fetch_when_disabled():
    sheet_payload = {"columns": [], "rows": [{"id": 1}], "totalRowCount": 1}
    client = _make_client(sheet_payload, [])

    await SheetService(client).get_sheet(123, include_attachments=False)

    client._fake_sdk.Attachments.list_all_attachments.assert_not_called()
