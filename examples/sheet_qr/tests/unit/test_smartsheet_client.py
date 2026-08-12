"""Smartsheet client: row reads that don't waste the quota, and cellimages
that sit inside the shared retry envelope.

Retry classification and rate limiting live in smarthub_core now. What this
file still owns is the SmartQR-shaped contract: column caching keyed by
identity, single-row reads that never pull a whole sheet, and the bounded
retry around a cellimages 1004 that distinguishes a flaky concurrent write
from a real permission refusal.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from smart_qr.adapters.smartsheet import client as ss_client
from smart_qr.adapters.smartsheet.client import LiveSmartsheet


class FakeSheets:
    def __init__(self):
        self.column_calls = 0
        self.row_calls = 0
        self.columns = SimpleNamespace(
            data=[
                SimpleNamespace(id=11, title="SKU"),
                SimpleNamespace(id=22, title="Name"),
            ]
        )
        self.row = SimpleNamespace(
            id=101,
            cells=[
                SimpleNamespace(column_id=11, value="A-1", image=None),
                SimpleNamespace(column_id=22, value="Widget", image=None),
            ],
            permalink="https://app.smartsheet.com/sheets/1?rowId=101",
        )

    def get_columns(self, sheet_id, include_all=False):
        self.column_calls += 1
        return self.columns

    def get_row(self, sheet_id, row_id, include=None):
        self.row_calls += 1
        return self.row

    def get_sheet(self, *a, **k):
        raise AssertionError("get_sheet must not be used for single-row reads")


class FakeCore:
    """Stands in for smarthub_core.SmartsheetClient.

    Runs the operation against a canned SDK client. Tests can make the next
    call raise by setting ``raise_next``.
    """

    def __init__(self, sheets: FakeSheets):
        self.org_id = "org-1"
        self._sheets = sheets
        self.raise_next: list[BaseException] = []
        self.calls: list[str] = []
        self.attachment_uploads: list[tuple[int, int, str, bytes]] = []
        self.attachment_deletes: list[tuple[int, int]] = []
        self.change_agents: list[str] = []
        self.row_attachments = [SimpleNamespace(id=7001, name="smartqr-22-101.png")]
        self._sdk = SimpleNamespace(
            Sheets=sheets,
            Attachments=SimpleNamespace(
                list_row_attachments=lambda _sheet_id, _row_id: SimpleNamespace(
                    data=list(self.row_attachments)
                ),
                attach_file_to_row=self._attach_file_to_row,
                delete_attachment=self._delete_attachment,
            ),
            prepare_request=lambda op: op,
            request=self._request,
            with_change_agent=self._with_change_agent,
        )
        self._change_agent: str | None = None

    def _with_change_agent(self, agent: str):
        self._change_agent = agent
        self.change_agents.append(agent)
        return self._sdk

    def _request(self, *_a, **_k):
        return SimpleNamespace(result=True)

    def _attach_file_to_row(self, sheet_id, row_id, file_tuple):
        filename, stream, _content_type = file_tuple
        self.attachment_uploads.append((sheet_id, row_id, filename, stream.read()))
        return SimpleNamespace(result=SimpleNamespace(id=8001))

    def _delete_attachment(self, sheet_id, attachment_id):
        self.attachment_deletes.append((sheet_id, attachment_id))
        return SimpleNamespace(result=True)

    async def call_with_retry_async(
        self, operation: Callable[[Any], Any], *, description: str = "", **_k
    ):
        self.calls.append(description)
        if self.raise_next:
            raise self.raise_next.pop(0)
        return operation(self._sdk)


@pytest.fixture
def live():
    ss_client._columns_cache.clear()
    sheets = FakeSheets()
    core = FakeCore(sheets)
    return LiveSmartsheet(core, "smart-qr-v1", cache_scope="token-a"), sheets, core


@pytest.mark.asyncio
async def test_row_reads_do_not_fetch_the_whole_sheet(live):
    client, sheets, _ = live
    cells = await client.get_row_content_cells(1, 101, ["SKU", "Name"])
    assert cells["SKU"].value == "A-1"
    assert cells["Name"].value == "Widget"
    assert sheets.row_calls == 1
    assert sheets.column_calls == 1


@pytest.mark.asyncio
async def test_column_titles_are_cached_across_rows(live):
    client, sheets, _ = live
    await client.get_row_content_cells(1, 101, ["SKU"])
    await client.get_row_content_cells(1, 101, ["Name"])
    assert sheets.column_calls == 1
    assert sheets.row_calls == 2


@pytest.mark.asyncio
async def test_a_renamed_column_triggers_one_refresh(live):
    client, sheets, _ = live
    await client.get_row_content_cells(1, 101, ["SKU"])
    sheets.columns.data.append(SimpleNamespace(id=33, title="New"))
    await client.get_row_content_cells(1, 101, ["New"])
    assert sheets.column_calls == 2


@pytest.mark.asyncio
async def test_column_cache_is_not_shared_across_identities():
    ss_client._columns_cache.clear()
    sheets_a, sheets_b = FakeSheets(), FakeSheets()
    a = LiveSmartsheet(FakeCore(sheets_a), "smart-qr-v1", cache_scope="user-a")
    b = LiveSmartsheet(FakeCore(sheets_b), "smart-qr-v1", cache_scope="user-b")
    await a.get_row_content_cells(1, 101, ["SKU"])
    await b.get_row_content_cells(1, 101, ["SKU"])
    assert sheets_a.column_calls == 1
    assert sheets_b.column_calls == 1


NOT_AUTHORIZED = (
    'status=403 body={"errorCode": 1004, "message": "You are not authorized"}'
)


@pytest.mark.asyncio
async def test_cellimages_retries_a_transient_1004_then_succeeds(live, monkeypatch):
    """SH-176: concurrent same-row writes can answer 1004 once and then work."""
    client, _, core = live
    sleeps: list[float] = []

    async def _record(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ss_client.asyncio, "sleep", _record)
    core.raise_next = [RuntimeError(NOT_AUTHORIZED)]

    await client.upload_cell_image(1, 101, 22, b"png", "smart-qr-v1")

    assert len(sleeps) == 1
    assert any("cellimages" in c for c in core.calls)


@pytest.mark.asyncio
async def test_cellimages_exhausted_1004_reports_not_authorized(live, monkeypatch):
    client, _, core = live

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(ss_client.asyncio, "sleep", _no_sleep)
    core.raise_next = [
        RuntimeError(NOT_AUTHORIZED),
        RuntimeError(NOT_AUTHORIZED),
        RuntimeError(NOT_AUTHORIZED),
    ]

    with pytest.raises(RuntimeError) as exc:
        await client.upload_cell_image(1, 101, 22, b"png", "smart-qr-v1")

    assert "not authorized" in str(exc.value)
    assert "WRITE_SHEETS" in str(exc.value)


@pytest.mark.asyncio
async def test_cellimages_non_1004_errors_are_not_retried_here(live):
    """Core already retried everything Smartsheet asks us to retry; a leftover
    503 is ours to report, not to loop on again."""
    client, _, core = live
    core.raise_next = [RuntimeError("cellimages failed: 503 gateway")]

    with pytest.raises(RuntimeError, match="503"):
        await client.upload_cell_image(1, 101, 22, b"png", "smart-qr-v1")

    # One attempt only — the 1004 loop must not wrap every other failure.
    assert len([c for c in core.calls if "cellimages" in c]) == 1


@pytest.mark.asyncio
async def test_row_attachment_crud_uses_core_retry_envelope(live):
    client, _, core = live

    assert await client.row_attachment_exists(1, 101, 7001) is True
    assert await client.row_attachment_exists(1, 101, 9999) is False

    attachment_id = await client.upload_row_attachment(
        1,
        101,
        b"png-bytes",
        "smart-qr-v1",
        "smartqr-22-101.png",
    )
    await client.delete_row_attachment(1, attachment_id, "smart-qr-v1")

    assert attachment_id == 8001
    assert core.attachment_uploads == [(1, 101, "smartqr-22-101.png", b"png-bytes")]
    assert core.attachment_deletes == [(1, 8001)]
    assert any("list_row_attachments" in call for call in core.calls)
    assert any("attach_file_to_row" in call for call in core.calls)
    assert any("delete_attachment" in call for call in core.calls)
    assert core.change_agents == ["smart-qr-v1", "smart-qr-v1"]
    assert core._change_agent == "smart-qr-v1"


@pytest.mark.asyncio
async def test_row_attachment_works_without_mutable_default_headers(live):
    """Staging crash path: SDK has no dict at default_headers (SH-232)."""
    client, _, core = live
    # Ensure we never rely on a mutable headers dict.
    assert not hasattr(core._sdk, "default_headers") or not isinstance(
        getattr(core._sdk, "default_headers", None), dict
    )
    attachment_id = await client.upload_row_attachment(
        1, 101, b"png", "smart-qr-v1", "smartqr-22-101.png"
    )
    assert attachment_id == 8001
    assert core.change_agents[-1] == "smart-qr-v1"


def test_apply_change_agent_skips_string_default_headers():
    """Guard the helper itself against the SDK __getattr__ trap."""
    from smart_qr.adapters.smartsheet.client import _apply_change_agent

    class Trap:
        def __getattr__(self, name):
            return name  # same as smartsheet.Smartsheet.__getattr__ fallback

    trap = Trap()
    # Must not raise TypeError on item assignment into the returned string.
    _apply_change_agent(trap, "smart-qr-v1")

    class WithHook:
        def __init__(self):
            self.seen = None

        def with_change_agent(self, agent):
            self.seen = agent

    hooked = WithHook()
    _apply_change_agent(hooked, "smart-qr-v1")
    assert hooked.seen == "smart-qr-v1"

    class DictHeaders:
        def __init__(self):
            self.default_headers = {}

    mapped = DictHeaders()
    _apply_change_agent(mapped, "smart-qr-v1")
    assert mapped.default_headers["Smartsheet-Change-Agent"] == "smart-qr-v1"
