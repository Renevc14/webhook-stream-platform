"""
Sheet Service
=============

Async wrapper for Smartsheet sheet operations.

SH-100: methods are now ``async def`` and route through
``smarthub_core.platforms.smartsheet.SmartsheetClient.call_with_retry_async``.
Every Smartsheet read this wrapper makes therefore picks up the cluster-wide
rate limit, the encrypted token cache on Redis, and the retry coverage for
``errorCode 4000`` / HTTP/2 ``ConnectionTerminated`` that the legacy
``SmartsheetService`` did not handle.

CPU-bound work that stays in the wrapper (extracting attachments from a
row dict, filtering by mime type) is left as a plain sync method —
no IO, no need to enter the event loop.
"""

import logging
from typing import List, Dict, Any

import smartsheet  # type: ignore[import-untyped]

from smarthub_core.platforms.smartsheet import SmartsheetClient

from src.models.attachment import Attachment


log = logging.getLogger(__name__)


class SheetService:
    """Async wrapper for Smartsheet sheet operations."""

    def __init__(self, client: SmartsheetClient):
        """Bind to a smarthub_core ``SmartsheetClient``.

        The client is org-scoped (constructed via
        :func:`src.services.smartsheet.client.get_smartsheet_client`) and
        carries the shared token cache + rate limit + retry. The wrapper
        holds it only as a reference; the SDK client itself is owned by
        the platform layer.
        """
        self._client = client

    async def get_sheet(
        self,
        sheet_id: int,
        max_pages: int = 10,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        """Fetch a sheet with pagination."""
        all_rows: List[Dict[str, Any]] = []
        columns = None
        page_number = 1
        page_size = 1000

        # SH-120: Smartsheet rejects `include=attachments` on the Get Sheet
        # endpoint with HTTP 400 / errorCode 1008 ("Unable to parse request")
        # as of 2026-05-29. The same option still works per-row, which is why
        # webhook processing is unaffected and only backfill (full-sheet scan)
        # broke. We fetch rows without the include and merge attachment
        # metadata from the dedicated List Attachments endpoint (still works).
        while page_number <= max_pages:
            # Capture page_number in the closure so retry sees the right page.
            page = page_number
            result = await self._client.call_with_retry_async(
                lambda c, _page=page: c.Sheets.get_sheet(
                    sheet_id, page_size=page_size, page=_page
                ),
                description=f"get_sheet({sheet_id}, page={page})",
            )

            sheet_dict = result.to_dict()

            if columns is None:
                columns = sheet_dict.get("columns", [])

            rows = sheet_dict.get("rows", [])
            if not rows:
                break

            all_rows.extend(rows)

            total_rows = sheet_dict.get("totalRowCount", len(all_rows))
            if len(all_rows) >= total_rows:
                break

            page_number += 1

        if include_attachments and all_rows:
            attachments_by_row = await self._list_attachments_by_row(sheet_id)
            for row in all_rows:
                row["attachments"] = attachments_by_row.get(row.get("id"), [])

        log.debug(
            f"Fetched {len(all_rows)} rows from sheet {sheet_id} ({page_number} pages)"
        )

        return {
            "id": sheet_id,
            "columns": columns,
            "rows": all_rows,
            "totalRowCount": len(all_rows),
        }

    async def _list_attachments_by_row(
        self, sheet_id: int
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Return all ROW-parented attachments for a sheet, grouped by row id.

        SH-120: replaces the `include=attachments` option on Get Sheet, which
        Smartsheet now rejects (HTTP 400 / errorCode 1008). The List Attachments
        endpoint still works and returns each attachment with `parentType` and
        `parentId`, so we can reattach them to their rows.
        """
        # Hotfix 2026-06-17: `include_all=True` maps to the deprecated
        # `includeAll` query parameter (errorCode 1018). Replaced with explicit
        # `page_size=10000` (the documented Smartsheet max page size).
        result = await self._client.call_with_retry_async(
            lambda c: c.Attachments.list_all_attachments(
                sheet_id, page_size=10000
            ),
            description=f"list_all_attachments(sheet={sheet_id})",
        )

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for att in result.to_dict().get("data", []):
            if (att.get("parentType") or "").upper() != "ROW":
                continue
            parent_id = att.get("parentId")
            if parent_id is None:
                continue
            grouped.setdefault(parent_id, []).append(att)
        return grouped

    async def get_row(
        self,
        sheet_id: int,
        row_id: int,
        include_attachments: bool = True,
    ) -> Dict[str, Any]:
        """Fetch a single row from a sheet."""
        include = ["attachments"] if include_attachments else []

        result = await self._client.call_with_retry_async(
            lambda c: c.Sheets.get_row(sheet_id, row_id, include=include),
            description=f"get_row(sheet={sheet_id}, row={row_id})",
        )

        return result.to_dict()

    @staticmethod
    def extract_row_attachments(
        row: Dict[str, Any],
        images_only: bool = True,
    ) -> List[Attachment]:
        """Extract Attachment models from a row dict. Pure / sync.

        Stays synchronous because it only manipulates the dict the caller
        already has in memory — no IO, no rate limit, no point of leaving
        the calling coroutine.

        ``@staticmethod`` so callers that only need this helper (notably
        :class:`src.core.row_processor.RowProcessor`) do not have to
        build a ``SmartsheetClient`` they will never actually use.
        ``sheet_service.extract_row_attachments(row)`` still works on
        instances unchanged.
        """
        attachments_data = row.get("attachments", [])
        if not attachments_data:
            return []

        attachments = []
        for att_data in attachments_data:
            att = Attachment(**att_data)

            if images_only:
                if att.is_processable:
                    attachments.append(att)
                else:
                    log.info(
                        f"[ATTACHMENT:SKIP] Non-processable: {att.name} "
                        f"(mime: {att.mime_type})"
                    )
            else:
                attachments.append(att)

        return attachments

    async def get_rows_with_attachments(
        self,
        sheet_id: int,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """Convenience: rows that have at least one image-processable attachment."""
        sheet = await self.get_sheet(
            sheet_id, max_pages=max_pages, include_attachments=True
        )

        rows_with_attachments = [
            row
            for row in sheet.get("rows", [])
            if self.extract_row_attachments(row)
        ]

        log.debug(
            f"Found {len(rows_with_attachments)} rows with attachments "
            f"in sheet {sheet_id}"
        )

        return rows_with_attachments

    async def get_columns(self, sheet_id: int) -> List[Dict[str, Any]]:
        """Fetch column definitions for a sheet."""
        # Hotfix 2026-06-17: `include_all=True` → deprecated includeAll (1018).
        # Smartsheet caps sheets at 400 columns; page_size=10000 covers all cases.
        result = await self._client.call_with_retry_async(
            lambda c: c.Sheets.get_columns(sheet_id, page_size=10000),
            description=f"get_columns(sheet={sheet_id})",
        )
        return result.to_dict().get("data", [])

    async def update_cell(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        value: Any,
    ) -> bool:
        """Update a single cell value."""

        def _do_update(c: smartsheet.Smartsheet):
            row = c.models.Row()
            row.id = row_id
            cell = c.models.Cell()
            cell.column_id = column_id
            cell.value = value
            row.cells.append(cell)
            return c.Sheets.update_rows(sheet_id, [row])

        result = await self._client.call_with_retry_async(
            _do_update,
            description=f"update_cell(sheet={sheet_id}, row={row_id}, col={column_id})",
        )
        return result.message == "SUCCESS"
