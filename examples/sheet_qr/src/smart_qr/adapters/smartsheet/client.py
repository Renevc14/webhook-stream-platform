"""Smartsheet access for SmartQR, on top of smarthub_core's shared client.

Every live call goes through ``smarthub_core.platforms.smartsheet.SmartsheetClient``,
which owns the three things that must not differ per tool:

* **Credential resolution** per (org, user) through the encrypt-tool broker.
* **Retry classification** — the patterns there were learned from production
  incidents across the platform (``errorCode 4000``, ``4004``, HTTP/2 GOAWAY).
* **Rate limiting** on a Redis sliding window, so a SmartQR backfill and an
  image-to-cell run cannot each burn an org's Smartsheet quota believing they
  are alone.

This module is left with what is genuinely SmartQR's: which cells to read, how
to decide a row has content, and how to push a PNG into a cell image.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from uuid import UUID

from smart_qr.adapters.smartsheet.credentials import (
    DEFAULT_API_BASE,  # noqa: F401  (re-exported: callers still build raw clients)
    CredentialResolver,
    get_credential_resolver,
)

log = logging.getLogger(__name__)

# Serialize cellimages per Smartsheet row (I2C SH-176 lesson)
_row_locks: dict[int, asyncio.Lock] = {}

# Column titles change rarely; a backfill reads them once per row otherwise.
COLUMNS_TTL_S = 300.0
_columns_cache: dict[tuple[int, str], tuple[float, dict[int, str]]] = {}

# SH-176 (image-to-cell): Smartsheet's /cellimages endpoint intermittently
# answers 1004 "not authorized" on concurrent same-row writes even when the
# token is fine. A real permission problem answers the same way every time, so
# a couple of bounded retries tell the two apart without looping on a verdict.
CELLIMAGES_ATTEMPTS = 3
CELLIMAGES_RETRY_BASE_S = 0.5


def _apply_change_agent(client: Any, agent: str) -> None:
    """Set Smartsheet-Change-Agent without touching the SDK's fake default_headers.

    Accessing ``client.default_headers`` triggers Smartsheet's ``__getattr__``,
    which tries to import an API module named ``default_headers``, logs
    ``ImportError! Could not load api or model class default_headers``, and
    returns the string ``\"default_headers\"``. Item assignment on that string
    then raises ``TypeError`` (SH-232). The SDK's supported hook is
    ``with_change_agent``; ``prepare_request`` injects the header from
    ``_change_agent``.
    """
    apply = getattr(client, "with_change_agent", None)
    if callable(apply):
        apply(agent)
        return
    # Test doubles may expose a real dict; never assign into a non-mapping.
    headers = getattr(client, "default_headers", None)
    if isinstance(headers, dict):
        headers["Smartsheet-Change-Agent"] = agent


def _lock_for_row(row_id: int) -> asyncio.Lock:
    if row_id not in _row_locks:
        _row_locks[row_id] = asyncio.Lock()
    return _row_locks[row_id]


def _looks_like_permission_refusal(exc: BaseException) -> bool:
    """True when Smartsheet answered 1004 "not authorized".

    Both markers are required: the error text carries our own description, which
    contains sheet and row ids that can contain "1004" by coincidence.
    """
    msg = str(exc).lower()
    return "1004" in msg and ("authorized" in msg or "errorcode" in msg)


@dataclass(frozen=True)
class ContentCell:
    """A source cell value used to build QR/barcode payload text."""

    value: str
    has_image: bool = False


class SmartsheetPort(Protocol):
    async def get_row_cell_values(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, str]: ...

    async def get_row_content_cells(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, ContentCell]: ...

    async def get_row_permalink(self, sheet_id: int, row_id: int) -> str: ...

    async def list_row_ids(self, sheet_id: int) -> list[int]: ...

    async def sheet_has_column(self, sheet_id: int, column_id: int) -> bool: ...

    async def row_has_content(
        self,
        sheet_id: int,
        row_id: int,
        *,
        exclude_column_ids: Optional[list[int]] = None,
    ) -> bool: ...

    async def target_has_image(
        self, sheet_id: int, row_id: int, column_id: int
    ) -> bool: ...

    async def upload_cell_image(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str = "smartqr.png",
    ) -> None: ...

    async def list_row_attachment_refs(
        self, sheet_id: int, row_id: int
    ) -> list[tuple[int, str]]: ...

    async def row_attachment_exists(
        self, sheet_id: int, row_id: int, attachment_id: int
    ) -> bool: ...

    async def upload_row_attachment(
        self,
        sheet_id: int,
        row_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str,
    ) -> int: ...

    async def delete_row_attachment(
        self, sheet_id: int, attachment_id: int, change_agent: str
    ) -> None: ...

    async def update_cell_value(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        value: str,
        change_agent: str,
    ) -> None: ...


class MockSmartsheet:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int, str], str] = {}
        self.images: dict[tuple[int, int, int], bytes] = {}
        self.attachments: dict[tuple[int, int, int], tuple[str, bytes]] = {}
        self._next_attachment_id = 1
        self.cell_values_by_id: dict[tuple[int, int, int], str] = {}
        self.row_ids: list[int] = [101, 102, 103]
        self.permalinks: dict[tuple[int, int], str] = {}
        # Rows treated as blank (no user content). Default: all rows have content.
        self.empty_rows: set[tuple[int, int]] = set()
        # Source cells that are cell images / attachments (title-keyed)
        self.image_cells: set[tuple[int, int, str]] = set()

    async def get_row_cell_values(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, str]:
        reads = await self.get_row_content_cells(sheet_id, row_id, column_titles)
        return {t: c.value for t, c in reads.items()}

    async def get_row_content_cells(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, ContentCell]:
        out: dict[str, ContentCell] = {}
        for t in column_titles:
            has_image = (sheet_id, row_id, t) in self.image_cells
            if has_image:
                # Mimic Smartsheet: image cells often expose the filename as value
                val = self.cells.get((sheet_id, row_id, t), f"attachment-{t}.png")
            else:
                val = self.cells.get((sheet_id, row_id, t), f"mock-{t}-{row_id}")
            out[t] = ContentCell(value="" if has_image else val, has_image=has_image)
        return out

    async def get_row_permalink(self, sheet_id: int, row_id: int) -> str:
        return self.permalinks.get(
            (sheet_id, row_id),
            f"https://app.smartsheet.com/sheets/{sheet_id}?rowId={row_id}",
        )

    async def list_row_ids(self, sheet_id: int) -> list[int]:
        return list(self.row_ids)

    async def sheet_has_column(self, sheet_id: int, column_id: int) -> bool:
        return True

    async def row_has_content(
        self,
        sheet_id: int,
        row_id: int,
        *,
        exclude_column_ids: Optional[list[int]] = None,
    ) -> bool:
        if (sheet_id, row_id) in self.empty_rows:
            return False
        # Explicit cell values for this row (if any were set)
        for (sid, rid, _title), val in self.cells.items():
            if sid == sheet_id and rid == row_id and str(val or "").strip():
                return True
        for (sid, rid, col_id), val in self.cell_values_by_id.items():
            if (
                sid == sheet_id
                and rid == row_id
                and (
                    not exclude_column_ids
                    or int(col_id) not in {int(x) for x in exclude_column_ids}
                )
                and str(val or "").strip()
            ):
                return True
        # Default for tests: row has content unless marked empty
        return True

    async def target_has_image(
        self, sheet_id: int, row_id: int, column_id: int
    ) -> bool:
        return (sheet_id, row_id, column_id) in self.images

    async def upload_cell_image(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str = "smartqr.png",
    ) -> None:
        async with _lock_for_row(row_id):
            self.images[(sheet_id, row_id, column_id)] = png_bytes
            log.info(
                "MockSmartsheet upload sheet=%s row=%s col=%s bytes=%s agent=%s",
                sheet_id,
                row_id,
                column_id,
                len(png_bytes),
                change_agent,
            )

    async def list_row_attachment_refs(
        self, sheet_id: int, row_id: int
    ) -> list[tuple[int, str]]:
        return [
            (aid, name)
            for (sid, rid, aid), (name, _bytes) in self.attachments.items()
            if sid == sheet_id and rid == row_id
        ]

    async def row_attachment_exists(
        self, sheet_id: int, row_id: int, attachment_id: int
    ) -> bool:
        return (sheet_id, row_id, attachment_id) in self.attachments

    async def upload_row_attachment(
        self,
        sheet_id: int,
        row_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str,
    ) -> int:
        attachment_id = self._next_attachment_id
        self._next_attachment_id += 1
        self.attachments[(sheet_id, row_id, attachment_id)] = (filename, png_bytes)
        log.info(
            "MockSmartsheet row attachment upload sheet=%s row=%s attachment=%s "
            "filename=%s bytes=%s agent=%s",
            sheet_id,
            row_id,
            attachment_id,
            filename,
            len(png_bytes),
            change_agent,
        )
        return attachment_id

    async def delete_row_attachment(
        self, sheet_id: int, attachment_id: int, change_agent: str
    ) -> None:
        for key in list(self.attachments):
            if key[0] == sheet_id and key[2] == attachment_id:
                del self.attachments[key]
                log.info(
                    "MockSmartsheet row attachment deleted sheet=%s attachment=%s agent=%s",
                    sheet_id,
                    attachment_id,
                    change_agent,
                )
                return

    async def update_cell_value(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        value: str,
        change_agent: str,
    ) -> None:
        self.cell_values_by_id[(sheet_id, row_id, column_id)] = value
        log.info(
            "MockSmartsheet update_cell sheet=%s row=%s col=%s value=%s agent=%s",
            sheet_id,
            row_id,
            column_id,
            value[:80],
            change_agent,
        )


class LiveSmartsheet:
    """Live Smartsheet through a core ``SmartsheetClient``.

    ``core`` already knows the organization, the product slug and the acting
    user, and resolves the regional API base itself — which is why nothing here
    handles tokens or hosts. ``cache_scope`` identifies those credentials for
    the process-wide column cache, so a cached map is never reused across
    identities that may not even see the same sheet.
    """

    def __init__(
        self,
        core: Any,
        change_agent_default: str,
        *,
        cache_scope: str = "",
    ) -> None:
        self._core = core
        self._change_agent_default = change_agent_default
        self._cache_scope = cache_scope or f"{getattr(core, 'org_id', '')}"

    async def _title_by_column_id(
        self, sheet_id: int, *, refresh: bool = False
    ) -> dict[int, str]:
        """Column id -> title, cached per sheet and identity.

        Cached because the alternative is fetching the whole sheet on every row
        read.
        """
        key = (int(sheet_id), self._cache_scope)
        hit = _columns_cache.get(key)
        now = time.monotonic()
        if hit and not refresh and now - hit[0] < COLUMNS_TTL_S:
            return hit[1]

        def _fetch(client: Any) -> dict[int, str]:
            columns = client.Sheets.get_columns(sheet_id, include_all=True)
            items = getattr(columns, "data", None) or []
            return {
                int(c.id): str(c.title)
                for c in items
                if getattr(c, "id", None) is not None
            }

        title_by_id = await self._core.call_with_retry_async(
            _fetch, description=f"get_columns(sheet={sheet_id})"
        )
        _columns_cache[key] = (now, title_by_id)
        return title_by_id

    async def get_row_cell_values(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, str]:
        reads = await self.get_row_content_cells(sheet_id, row_id, column_titles)
        return {t: c.value for t, c in reads.items()}

    async def _get_row(self, sheet_id: int, row_id: int) -> Any:
        return await self._core.call_with_retry_async(
            lambda client: client.Sheets.get_row(int(sheet_id), int(row_id)),
            description=f"get_row(sheet={sheet_id},row={row_id})",
        )

    async def get_row_content_cells(
        self, sheet_id: int, row_id: int, column_titles: list[str]
    ) -> dict[str, ContentCell]:
        wanted = set(column_titles)
        title_by_id = await self._title_by_column_id(sheet_id)
        # A column the config points at may have been renamed since; one refresh
        # tells us whether the map is stale before we report the cell as empty.
        if wanted - set(title_by_id.values()):
            title_by_id = await self._title_by_column_id(sheet_id, refresh=True)

        row = await self._get_row(sheet_id, row_id)
        out: dict[str, ContentCell] = {
            t: ContentCell(value="", has_image=False) for t in column_titles
        }
        for cell in getattr(row, "cells", None) or []:
            title = title_by_id.get(int(getattr(cell, "column_id", 0) or 0))
            if title not in wanted:
                continue
            if getattr(cell, "image", None) is not None:
                # Do not use attachment/image filename as encode text
                out[title] = ContentCell(value="", has_image=True)
                log.info(
                    "Source cell is image sheet=%s row=%s col=%s",
                    sheet_id,
                    row_id,
                    title,
                )
                continue
            raw = getattr(cell, "value", None)
            out[title] = ContentCell(
                value="" if raw is None else str(raw), has_image=False
            )
        return out

    async def get_row_permalink(self, sheet_id: int, row_id: int) -> str:
        def _fetch(client: Any) -> str:
            try:
                row = client.Sheets.get_row(sheet_id, row_id, include=["rowPermalink"])
            except TypeError:
                row = client.Sheets.get_row(sheet_id, row_id, include="rowPermalink")
            permalink = getattr(row, "permalink", None) or getattr(
                row, "perma_link", None
            )
            if not permalink and hasattr(row, "to_dict"):
                permalink = (row.to_dict() or {}).get("permalink") or (
                    row.to_dict() or {}
                ).get("permaLink")
            if not permalink:
                raise RuntimeError(
                    f"Smartsheet did not return rowPermalink for sheet={sheet_id} row={row_id}"
                )
            return str(permalink)

        return await self._core.call_with_retry_async(
            _fetch, description=f"get_row_permalink(row={row_id})"
        )

    async def list_row_ids(self, sheet_id: int) -> list[int]:
        def _fetch(client: Any) -> list[int]:
            page = 1
            out: list[int] = []
            while True:
                try:
                    sheet = client.Sheets.get_sheet(sheet_id, page_size=500, page=page)
                except TypeError:
                    sheet = client.Sheets.get_sheet(sheet_id)
                    rows = list(sheet.rows or [])
                    return [
                        int(r.id) for r in rows if getattr(r, "id", None) is not None
                    ]
                rows = list(sheet.rows or [])
                for r in rows:
                    if getattr(r, "id", None) is not None:
                        out.append(int(r.id))
                total = getattr(sheet, "total_row_count", None)
                if not rows:
                    break
                if total is not None and len(out) >= int(total):
                    break
                if len(rows) < 500:
                    break
                page += 1
                if page > 50:
                    break
            log.info("Listed %s row ids for sheet=%s", len(out), sheet_id)
            return out

        return await self._core.call_with_retry_async(
            _fetch, description=f"list_row_ids(sheet={sheet_id})"
        )

    async def sheet_has_column(self, sheet_id: int, column_id: int) -> bool:
        want = int(column_id)
        title_by_id = await self._title_by_column_id(sheet_id)
        if want in title_by_id:
            return True
        # Deleted columns are the reason this check exists, so never answer "no"
        # from a cached map.
        if want in await self._title_by_column_id(sheet_id, refresh=True):
            return True
        log.warning(
            "Target column missing sheet=%s column_id=%s",
            sheet_id,
            column_id,
        )
        return False

    async def row_has_content(
        self,
        sheet_id: int,
        row_id: int,
        *,
        exclude_column_ids: Optional[list[int]] = None,
    ) -> bool:
        """True when the row has any non-blank cell value (excl. image target)."""

        excluded = {int(x) for x in (exclude_column_ids or [])}
        row = await self._get_row(sheet_id, row_id)

        def _has_content() -> bool:
            for cell in getattr(row, "cells", None) or []:
                col_id = getattr(cell, "column_id", None)
                if col_id is not None and int(col_id) in excluded:
                    continue
                raw = getattr(cell, "value", None)
                if raw is None:
                    raw = getattr(cell, "display_value", None) or getattr(
                        cell, "displayValue", None
                    )
                if raw is None:
                    continue
                if isinstance(raw, str) and not raw.strip():
                    continue
                return True
            return False

        has = _has_content()
        if not has:
            log.info(
                "Row has no user content sheet=%s row=%s (exclude_cols=%s)",
                sheet_id,
                row_id,
                exclude_column_ids,
            )
        return has

    async def target_has_image(
        self, sheet_id: int, row_id: int, column_id: int
    ) -> bool:
        row = await self._get_row(sheet_id, row_id)
        want = int(column_id)
        for cell in getattr(row, "cells", None) or []:
            if int(getattr(cell, "column_id", 0) or 0) == want:
                return getattr(cell, "image", None) is not None
        return False

    async def upload_cell_image(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str = "smartqr.png",
    ) -> None:
        """Push PNG bytes into a cell image.

        The SDK exposes no cellimages route, so this drives a raw operation
        through the SDK's own request path — which keeps the upload inside core's
        retry, rate-limit and token-refresh envelope instead of opening a second
        HTTP client that knows nothing about either.
        """
        from smartsheet import fresh_operation  # type: ignore[import-untyped]

        # Smartsheet cellimages expects a raw body + Content-Disposition, not
        # multipart form fields — multipart triggers errorCode 1011.
        safe_name = filename.replace('"', "")
        agent = change_agent or self._change_agent_default

        def _upload(client: Any) -> Any:
            op = fresh_operation("attach_file_to_cell")
            op["method"] = "POST"
            op["path"] = (
                f"/sheets/{sheet_id}/rows/{row_id}/columns/{column_id}/cellimages"
            )
            op["headers"] = {
                "content-type": "image/png",
                "content-disposition": f'attachment; filename="{safe_name}"',
                "Smartsheet-Change-Agent": agent,
            }
            op["form_data"] = png_bytes
            prepped = client.prepare_request(op)
            return client.request(prepped, ["Result", "Row"], op)

        description = f"cellimages(sheet={sheet_id},row={row_id},col={column_id})"

        async with _lock_for_row(row_id):
            for attempt in range(CELLIMAGES_ATTEMPTS):
                try:
                    await self._core.call_with_retry_async(
                        _upload, description=description
                    )
                    log.info(
                        "cellimages OK sheet=%s row=%s col=%s bytes=%s",
                        sheet_id,
                        row_id,
                        column_id,
                        len(png_bytes),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    if not _looks_like_permission_refusal(exc):
                        # Core already retried everything Smartsheet asks us to
                        # retry; anything left is ours to report.
                        raise
                    if attempt < CELLIMAGES_ATTEMPTS - 1:
                        delay = CELLIMAGES_RETRY_BASE_S * (2**attempt)
                        log.warning(
                            "cellimages 1004 sheet=%s row=%s col=%s "
                            "(attempt %s/%s, retry in %.1fs): %s",
                            sheet_id,
                            row_id,
                            column_id,
                            attempt + 1,
                            CELLIMAGES_ATTEMPTS,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                        continue
                    log.error(
                        "cellimages not authorized sheet=%s row=%s col=%s: %s",
                        sheet_id,
                        row_id,
                        column_id,
                        exc,
                    )
                    raise RuntimeError(
                        "cellimages not authorized: the connected Smartsheet account "
                        "cannot write images to this sheet (missing WRITE_SHEETS "
                        f"consent or edit access) — {exc}"
                    ) from exc

    async def list_row_attachment_refs(
        self, sheet_id: int, row_id: int
    ) -> list[tuple[int, str]]:
        """Return ``(attachment_id, filename)`` for every file on the row."""

        def _list(client: Any) -> list[Any]:
            result = client.Attachments.list_row_attachments(sheet_id, row_id)
            items = getattr(result, "data", None)
            if items is not None:
                return list(items)
            if hasattr(result, "to_dict"):
                return list((result.to_dict() or {}).get("data") or [])
            return []

        items = await self._core.call_with_retry_async(
            _list,
            description=f"list_row_attachments(sheet={sheet_id},row={row_id})",
        )
        refs: list[tuple[int, str]] = []
        for item in items:
            if isinstance(item, dict):
                raw_id = item.get("id")
                name = item.get("name") or ""
            else:
                raw_id = getattr(item, "id", None)
                name = getattr(item, "name", None) or ""
            if raw_id is None:
                continue
            refs.append((int(raw_id), str(name)))
        return refs

    async def row_attachment_exists(
        self, sheet_id: int, row_id: int, attachment_id: int
    ) -> bool:
        """Confirm the SmartQR-owned attachment still belongs to this row."""
        refs = await self.list_row_attachment_refs(sheet_id, row_id)
        want = int(attachment_id)
        return any(aid == want for aid, _name in refs)

    async def upload_row_attachment(
        self,
        sheet_id: int,
        row_id: int,
        png_bytes: bytes,
        change_agent: str,
        filename: str,
    ) -> int:
        """Attach the rendered PNG to a row through core's retry envelope."""
        safe_name = filename.replace('"', "")
        agent = change_agent or self._change_agent_default

        def _upload(client: Any) -> Any:
            # SDK has no mutable default_headers dict — __getattr__ returns the
            # string "default_headers" and item assignment crashes (SH-232).
            # with_change_agent is the supported hook; prepare_request injects it.
            _apply_change_agent(client, agent)
            # Construct a fresh stream on every core retry: the SDK consumes it.
            stream = io.BytesIO(png_bytes)
            return client.Attachments.attach_file_to_row(
                sheet_id,
                row_id,
                (safe_name, stream, "image/png"),
            )

        result = await self._core.call_with_retry_async(
            _upload,
            description=(
                f"attach_file_to_row(sheet={sheet_id},row={row_id},filename={safe_name})"
            ),
        )
        payload = getattr(result, "result", None) or result
        attachment_id = getattr(payload, "id", None)
        if attachment_id is None and hasattr(payload, "to_dict"):
            attachment_id = (payload.to_dict() or {}).get("id")
        if attachment_id is None and hasattr(result, "to_dict"):
            result_dict = result.to_dict() or {}
            inner = result_dict.get("result") or result_dict
            attachment_id = inner.get("id") if isinstance(inner, dict) else None
        if attachment_id is None:
            raise RuntimeError(
                f"Smartsheet attachment upload returned no id for sheet={sheet_id} row={row_id}"
            )
        log.info(
            "row attachment OK sheet=%s row=%s attachment=%s filename=%s bytes=%s",
            sheet_id,
            row_id,
            attachment_id,
            safe_name,
            len(png_bytes),
        )
        return int(attachment_id)

    async def delete_row_attachment(
        self, sheet_id: int, attachment_id: int, change_agent: str
    ) -> None:
        """Delete one attachment previously recorded as SmartQR-owned."""
        agent = change_agent or self._change_agent_default

        def _delete(client: Any) -> Any:
            _apply_change_agent(client, agent)
            return client.Attachments.delete_attachment(sheet_id, attachment_id)

        await self._core.call_with_retry_async(
            _delete,
            description=(
                f"delete_attachment(sheet={sheet_id},attachment={attachment_id})"
            ),
        )
        log.info(
            "row attachment deleted sheet=%s attachment=%s",
            sheet_id,
            attachment_id,
        )

    async def update_cell_value(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        value: str,
        change_agent: str,
    ) -> None:
        """Write a text/date cell value with Change-Agent (retryable).

        Setting a cell value is idempotent, so a retry after an ambiguous
        failure cannot double-apply.
        """
        import smartsheet

        agent = change_agent or self._change_agent_default

        def _update(client: Any) -> None:
            _apply_change_agent(client, agent)
            row = smartsheet.models.Row()
            row.id = int(row_id)
            cell = smartsheet.models.Cell()
            cell.column_id = int(column_id)
            cell.value = value
            cell.strict = False
            row.cells = [cell]
            client.Sheets.update_rows(sheet_id, [row])
            log.info(
                "update_cell OK sheet=%s row=%s col=%s",
                sheet_id,
                row_id,
                column_id,
            )

        await self._core.call_with_retry_async(
            _update,
            description=f"update_cell(sheet={sheet_id},row={row_id},col={column_id})",
        )


class SmartsheetProvider(Protocol):
    """Hands out a client bound to one organization's credentials.

    Every caller must go through this: a process-wide client would mean one
    org's token writing into another org's sheets.
    """

    async def get(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> SmartsheetPort: ...


class StaticSmartsheetProvider:
    """Always the same port — mock mode and tests."""

    def __init__(self, port: SmartsheetPort) -> None:
        self._port = port

    async def get(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> SmartsheetPort:
        return self._port


class OAuthSmartsheetProvider:
    """Builds a live client for the credentials of one (org, user).

    Credentials are resolved before handing the port over, even though the core
    client would resolve them on its first call: only this path can tell
    "not connected" from "revoked" from "the broker is down", and callers map
    those three to different answers. The resolution is cached, so the check
    costs a broker round trip about once a minute per identity — which is also
    what bounds how long a revoked grant keeps working.
    """

    def __init__(
        self,
        settings: Any,
        resolver: Optional[CredentialResolver] = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver or get_credential_resolver(settings)

    async def get(
        self, organization_id: UUID, user_id: Optional[str] = None
    ) -> SmartsheetPort:
        from smart_qr.adapters.core_infra import get_smartsheet_client

        # Resolve up front so missing / revoked / temporary map to typed
        # SmartQR errors before the first API call. The core client resolves
        # again on its first call, but that hits the broker cache.
        await self._resolver.resolve(organization_id, user_id)
        core = await get_smartsheet_client(
            self._settings,
            organization_id,
            user_id,
            credential_resolver=self._resolver,
        )
        return LiveSmartsheet(
            core,
            self._settings.change_agent_id,
            cache_scope=f"{organization_id}:{user_id or ''}",
        )


def get_smartsheet_provider(settings: Any) -> SmartsheetProvider:
    if settings.smartsheet_mode == "mock":
        return StaticSmartsheetProvider(MockSmartsheet())
    return OAuthSmartsheetProvider(settings)
