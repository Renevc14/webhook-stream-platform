"""SH-176 — name_map rows silently drop images under concurrent cellimages writes.

Beth incident (2026-07-07, sheet 695569967173508, config "PG Inventory - Name
Map"): rows with two image columns intermittently lost one image with a spurious
Smartsheet ``1004`` ("You are not authorized"). The token clearly had edit
rights (dozens of successes on the same sheet); the failing column alternated
per row/instant, which is impossible for a real permission error.

Root cause: ``ImageHandler.process_batch`` uploaded a row's images concurrently
(``asyncio.gather``), so two ``POST .../cellimages`` calls hit the **same row**
within tens of milliseconds. Smartsheet's cellimages endpoint rejects concurrent
same-row writes with a transient ``1004``, and ``1004`` is non-retryable by
design — so the losing image was dropped permanently.

This module pins the post-fix contract:

  1. Uploads to the same row are **serialized** (never overlap).
  2. The i2c-3 cellimages upload path retries a transient ``1004`` (defense in
     depth for residual Smartsheet flakiness).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


_CELLIMAGES_1004 = (
    '{"result": {"code": 1004, "errorCode": 1004, "message": "You are not '
    'authorized to perform this action.", "statusCode": 403, '
    '"shouldRetry": false}}'
)


def _make_attachment(name: str) -> MagicMock:
    att = MagicMock()
    att.id = abs(hash(name)) % (10**9)
    att.name = name
    att.mime_type = "image/jpeg"
    att.signature = name
    return att


class _FakeCellImagesEndpoint:
    """Models Smartsheet /cellimages rejecting concurrent same-row writes.

    Any upload that observes another in-flight upload to the *same* row fails
    with a 1004, mirroring the production race. When uploads are serialized the
    per-row concurrency never exceeds 1 and every upload succeeds.
    """

    def __init__(self, overlap_s: float = 0.02) -> None:
        self._active_by_row: dict[int, int] = {}
        self.max_concurrency_by_row: dict[int, int] = {}
        self.successful_uploads: list[tuple[int, int]] = []
        self._overlap_s = overlap_s

    async def upload_to_cell(
        self,
        sheet_id: int,
        row_id: int,
        column_id: int,
        file_path: str,
        mime_type: str = "image/jpeg",
    ) -> bool:
        active = self._active_by_row.get(row_id, 0) + 1
        self._active_by_row[row_id] = active
        self.max_concurrency_by_row[row_id] = max(
            self.max_concurrency_by_row.get(row_id, 0), active
        )
        collided = active > 1
        try:
            await asyncio.sleep(self._overlap_s)
        finally:
            self._active_by_row[row_id] -= 1

        if collided:
            raise Exception(_CELLIMAGES_1004)

        self.successful_uploads.append((row_id, column_id))
        return True


def _build_image_handler(attachment_service: MagicMock) -> "object":
    """Construct an ImageHandler bypassing __init__ (no Redis/SmartsheetClient).

    Only the collaborators exercised by ``process_batch`` are wired.
    """
    from src.core.image_processor import ImageHandler

    handler = ImageHandler.__new__(ImageHandler)
    handler._organization_id = "org-test"
    handler._settings = MagicMock(max_parallel_images=5)
    handler._attachment_service = attachment_service

    # Conversion is CPU-bound and irrelevant here — return fixed bytes.
    converter = MagicMock()
    converter.process = MagicMock(return_value=b"converted-bytes")
    handler._converter = converter
    return handler


def _make_temp_manager() -> MagicMock:
    tm = MagicMock()
    tm.create_temp_file = MagicMock(return_value="/tmp/fake-output.jpg")
    return tm


@pytest.mark.asyncio
async def test_same_row_uploads_are_serialized_and_no_image_lost():
    """A 2-column name_map row must not upload concurrently, and both images
    must land (pre-fix this dropped one image to a spurious 1004)."""
    endpoint = _FakeCellImagesEndpoint()

    attachment_service = MagicMock()
    download = MagicMock()
    download.data = b"raw"
    download.size_bytes = 3
    attachment_service.download = AsyncMock(return_value=download)
    # Route uploads through the concurrency-modeling fake endpoint.
    attachment_service.upload_to_cell = endpoint.upload_to_cell

    handler = _build_image_handler(attachment_service)

    sheet_id = 695569967173508
    row_id = 4891347358777220
    attachments = [_make_attachment("Bar Code.jpeg"), _make_attachment("Asset.jpeg")]
    # name_map mapping: attachment idx -> column id
    column_mapping = {0: 111111111111, 1: 222222222222}

    results = await handler.process_batch(
        attachments,
        sheet_id,
        row_id,
        column_mapping,
        _make_temp_manager(),
        parallel=True,
    )

    # Root-cause guard: uploads to the same row never overlapped.
    assert endpoint.max_concurrency_by_row.get(row_id) == 1, (
        "cellimages uploads to the same row must be serialized"
    )
    # Behavioural guard: no image silently dropped.
    assert len(results) == 2
    assert all(r.success for r in results), (
        f"expected both images to upload, got: "
        f"{[(r.filename, r.success, r.error) for r in results]}"
    )
    assert len(endpoint.successful_uploads) == 2


@pytest.mark.asyncio
async def test_cellimages_upload_retries_transient_1004():
    """Defense in depth: the i2c-3 upload path retries a transient 1004 and
    succeeds on a later attempt instead of dropping the image."""
    from src.services.smartsheet.attachments import AttachmentService

    attempts = {"count": 0}

    class _FlakyClient:
        async def call_with_retry_async(self, operation, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                # First cellimages attempt returns the spurious 1004.
                raise Exception(f"upload failed: {_CELLIMAGES_1004}")
            result = MagicMock()
            result.result = MagicMock()
            return result

    svc = AttachmentService(_FlakyClient())

    ok = await svc.upload_bytes_to_cell(
        sheet_id=1,
        row_id=2,
        column_id=3,
        data=b"img",
        filename="Bar Code.jpeg",
    )

    assert ok is True
    assert attempts["count"] >= 2, "a transient 1004 must be retried, not dropped"


@pytest.mark.asyncio
async def test_cellimages_upload_gives_up_on_persistent_1004():
    """A genuinely unauthorized target keeps failing — retry is bounded and the
    error still surfaces (so DLQ / logs see it)."""
    from src.services.smartsheet.attachments import AttachmentService

    attempts = {"count": 0}

    class _AlwaysDeniedClient:
        async def call_with_retry_async(self, operation, **kwargs):
            attempts["count"] += 1
            raise Exception(f"upload failed: {_CELLIMAGES_1004}")

    svc = AttachmentService(_AlwaysDeniedClient())

    with pytest.raises(Exception):
        await svc.upload_bytes_to_cell(
            sheet_id=1,
            row_id=2,
            column_id=3,
            data=b"img",
            filename="Bar Code.jpeg",
        )

    # Bounded: initial try + a small, finite number of retries.
    assert 2 <= attempts["count"] <= 4
