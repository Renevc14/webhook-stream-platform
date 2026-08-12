"""
Attachment Service
==================

Async wrapper for Smartsheet attachment operations.

SH-100 (slice 2/3): every Smartsheet SDK call routes through
``smarthub_core.platforms.smartsheet.SmartsheetClient.call_with_retry_async``.
Local HTTP downloads and file IO stay synchronous internally but are wrapped
in ``asyncio.to_thread`` so the public surface is consistently ``async`` and
the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import os
import logging
from typing import Optional, List

import smartsheet  # type: ignore[import-untyped]

from smarthub_core.platforms.smartsheet import SmartsheetClient

from src.models.attachment import Attachment, AttachmentDownload
from src.utils.http_client import HttpClient, get_http_client


log = logging.getLogger(__name__)

MAX_ATTACHMENT_SIZE_MB = 50
MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024

# SH-176: Smartsheet's /cellimages endpoint intermittently returns a transient
# `1004` ("not authorized") — observed in prod on concurrent same-row writes and
# other flakiness. `1004` is non-retryable in the shared client by design (a
# real permission error must not loop), so we add a small, bounded retry scoped
# to this upload path only. A genuinely unauthorized target still surfaces after
# the retries are exhausted (logged + raised → DLQ).
CELLIMAGES_1004_MAX_RETRIES = 2
CELLIMAGES_1004_BASE_DELAY_S = 0.5


def _is_transient_1004(exc: BaseException) -> bool:
    """True when the exception looks like a Smartsheet `1004` cellimages error."""
    msg = str(exc).lower()
    return "1004" in msg and (
        "authorized" in msg or "errorcode" in msg or '"code": 1004' in msg
    )


class AttachmentService:
    """Async wrapper for Smartsheet attachment operations."""

    def __init__(
        self,
        client: SmartsheetClient,
        http_client: Optional[HttpClient] = None,
    ):
        """Bind to a smarthub_core ``SmartsheetClient``.

        Args:
            client: ``SmartsheetClient`` instance scoped to ``(org, slug)``.
            http_client: HTTP client for binary downloads. Uses the
                shared singleton when not provided. The HTTP layer is still
                synchronous; calls are dispatched to a thread so the
                coroutine that owns the wrapper does not block.
        """
        self._client = client
        self._http = http_client or get_http_client()

    async def get_attachment_metadata(
        self,
        sheet_id: int,
        attachment_id: int,
    ) -> dict:
        """Fetch attachment metadata including ``parentId`` and ``parentType``."""
        result = await self._client.call_with_retry_async(
            lambda c: c.Attachments.get_attachment(sheet_id, attachment_id),
            description=f"get_attachment(sheet={sheet_id}, attachment={attachment_id})",
        )
        return result.to_dict()

    async def get_attachment_url(
        self,
        sheet_id: int,
        attachment_id: int,
    ) -> Optional[str]:
        """Return the time-limited download URL for an attachment."""
        meta = await self.get_attachment_metadata(sheet_id, attachment_id)
        return meta.get("downloadUrl") or meta.get("url")

    async def download(
        self,
        sheet_id: int,
        attachment: Attachment,
    ) -> AttachmentDownload:
        """Resolve the URL (if needed) and download the attachment bytes.

        The actual HTTP transfer is dispatched to a thread so the event
        loop is not blocked while waiting on the wire.
        """
        if attachment.size_in_kb is not None:
            size_bytes_est = attachment.size_in_kb * 1024
            if size_bytes_est > MAX_ATTACHMENT_SIZE_BYTES:
                raise ValueError(
                    f"Attachment '{attachment.name}' is too large: "
                    f"{attachment.size_in_kb / 1024:.1f} MB "
                    f"(limit {MAX_ATTACHMENT_SIZE_MB} MB)"
                )

        url = attachment.effective_url
        if not url:
            url = await self.get_attachment_url(sheet_id, attachment.id)

        # Sync HTTP client is offloaded to a thread to keep the loop
        # responsive during the (potentially slow) download.
        data = await asyncio.to_thread(self._http.download, url)

        if len(data) > MAX_ATTACHMENT_SIZE_BYTES:
            raise ValueError(
                f"Attachment '{attachment.name}' is too large: "
                f"{len(data) / (1024 * 1024):.1f} MB "
                f"(limit {MAX_ATTACHMENT_SIZE_MB} MB)"
            )

        return AttachmentDownload(
            attachment=attachment,
            data=data,
            size_bytes=len(data),
        )

    async def upload_to_cell(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        file_path: str,
        mime_type: str = "image/jpeg",
    ) -> bool:
        """Read a local file and upload its bytes to a cell."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        filename = os.path.basename(file_path)
        # Disk IO is sync; offload so the loop stays responsive while reading
        # potentially large files.
        file_content = await asyncio.to_thread(_read_file_bytes, file_path)

        return await self.upload_bytes_to_cell(
            sheet_id, row_id, column_id, file_content, filename, mime_type
        )

    async def upload_bytes_to_cell(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        data: bytes,
        filename: str,
        mime_type: str = "image/jpeg",
    ) -> bool:
        """Upload image bytes directly to a cell using the ``cellimages`` endpoint.

        Uses the raw Smartsheet API path because the SDK does not expose
        the cellimages route at the time of writing. Runs entirely inside
        the SmartsheetClient retry + rate-limit envelope.
        """
        from smartsheet import fresh_operation  # type: ignore[import-untyped]
        from src.config.constants import CHANGE_AGENT_ID

        log.debug(
            f"Uploading {filename} ({len(data)} bytes) to "
            f"{sheet_id}/{row_id}/{column_id}"
        )

        def _do_upload(c: smartsheet.Smartsheet) -> object:
            _op = fresh_operation("attach_file_to_cell")
            _op["method"] = "POST"
            _op["path"] = (
                f"/sheets/{sheet_id}/rows/{row_id}/columns/{column_id}/cellimages"
            )
            _op["headers"] = {
                "content-type": mime_type,
                "content-disposition": f'attachment; filename="{filename}"',
                "Smartsheet-Change-Agent": CHANGE_AGENT_ID,
            }
            _op["form_data"] = data
            expected = ["Result", "Row"]
            prepped_request = c.prepare_request(_op)
            return c.request(prepped_request, expected, _op)

        result = None
        for attempt in range(CELLIMAGES_1004_MAX_RETRIES + 1):
            try:
                result = await self._client.call_with_retry_async(
                    _do_upload,
                    description=(
                        f"upload {filename} → cell {sheet_id}/{row_id}/{column_id}"
                    ),
                )
                break
            except Exception as e:
                # SH-176: retry a transient cellimages 1004 a bounded number of
                # times. Non-1004 errors (and exhausted 1004 retries) propagate.
                if _is_transient_1004(e) and attempt < CELLIMAGES_1004_MAX_RETRIES:
                    delay = CELLIMAGES_1004_BASE_DELAY_S * (2 ** attempt)
                    log.warning(
                        "Transient 1004 uploading %s to cell %s/%s/%s "
                        "(attempt %d/%d), retrying in %.1fs: %s",
                        filename, sheet_id, row_id, column_id,
                        attempt + 1, CELLIMAGES_1004_MAX_RETRIES + 1, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.error(f"Failed to upload image to cell: {e}")
                raise

        if result and hasattr(result, "result") and result.result:
            log.info(f"Successfully uploaded {filename} to cell")
            return True

        log.warning(f"Upload returned empty result for {filename}")
        return False

    async def delete_row_attachments(
        self,
        sheet_id: int,
        row_id: int,
    ) -> int:
        """Delete every attachment currently on the given row."""
        list_result = await self._client.call_with_retry_async(
            lambda c: c.Attachments.list_row_attachments(sheet_id, row_id),
            description=f"list_row_attachments(sheet={sheet_id}, row={row_id})",
        )
        attachments = list_result.to_dict().get("data", [])

        deleted = 0
        for att in attachments:
            try:
                await self._client.call_with_retry_async(
                    lambda c, att_id=att["id"]: c.Attachments.delete_attachment(
                        sheet_id, att_id
                    ),
                    description=(
                        f"delete_attachment(sheet={sheet_id}, "
                        f"attachment={att['id']})"
                    ),
                )
                deleted += 1
            except Exception as e:
                log.warning(f"Failed to delete attachment {att['id']}: {e}")

        return deleted

    async def get_row_attachments(
        self,
        sheet_id: int,
        row_id: int,
    ) -> List[Attachment]:
        """Return all attachments currently on the given row as model objects."""
        result = await self._client.call_with_retry_async(
            lambda c: c.Attachments.list_row_attachments(sheet_id, row_id),
            description=f"list_row_attachments(sheet={sheet_id}, row={row_id})",
        )
        data = result.to_dict().get("data", [])
        return [Attachment(**att) for att in data]


def _read_file_bytes(path: str) -> bytes:
    """Read a file's bytes — split out so ``asyncio.to_thread`` has a named
    target rather than a lambda (helps in traces and stack frames)."""
    with open(path, "rb") as f:
        return f.read()
