"""
Image Handler
=============

Handles individual image processing: download, convert, upload.

SH-100 (slice 2/3): ``AttachmentService`` is now async-first, so the
attachment download + upload calls inside ``process`` go through an
``asyncio.run`` bridge. SH-101 removes the bridge when the rest of the
core becomes async too.

The constructor used to take a ``SmartsheetService`` and pass it
straight into ``AttachmentService``. Now it takes the ``organization_id``
and constructs the new ``SmartsheetClient`` internally — the legacy
``SmartsheetService`` field was set but never read, so dropping it is
silent.
"""

import asyncio
import logging
import time
from typing import Optional, Tuple

from src.models.attachment import Attachment
from src.models.results import ImageResult
from src.services.smartsheet.attachments import AttachmentService
from src.services.smartsheet.client import get_smartsheet_client
from src.utils.image_utils import ImageConverter, TempFileManager, ImageProcessingError
from src.config.settings import Settings


log = logging.getLogger(__name__)


class ImageHandler:
    """Handles processing of individual images."""

    def __init__(
        self,
        organization_id: Optional[str],
        settings: Settings,
        owner_user_id: Optional[str] = None,
    ):
        """Initialize image handler.

        Args:
            organization_id: Organization UUID used to scope the
                ``SmartsheetClient`` (and therefore the encrypted token
                cache + cluster-wide rate limit). May be ``None`` in
                unit tests that mock the handler — in that case
                ``_attachment_service`` is also ``None`` and any real
                ``process(...)`` call will fail loudly rather than
                silently use a wrong org.
            settings: Application settings.
        """
        self._organization_id = organization_id
        self._settings = settings
        self._attachment_service: Optional[AttachmentService] = (
            AttachmentService(get_smartsheet_client(organization_id, owner_user_id))
            if organization_id
            else None
        )
        self._converter = ImageConverter(
            target_max_px=settings.image_target_max_px,
            jpeg_qualities=settings.jpeg_qualities,
        )

    async def process(
        self,
        attachment: Attachment,
        sheet_id: int,
        row_id: int,
        column_id: int,
        temp_manager: TempFileManager,
        upload_lock: Optional[asyncio.Lock] = None,
    ) -> ImageResult:
        """Process a single image: download, convert, and upload.

        SH-101: now ``async def``. The AttachmentService calls are awaited
        directly (no ``asyncio.run`` bridge). CPU-bound conversion stays in
        ``asyncio.to_thread`` so PIL / PyMuPDF does not block the loop.

        SH-176: ``upload_lock`` serializes the ``cellimages`` POST across the
        images of a single row. Download + conversion stay parallel, but the
        upload itself must not overlap another upload to the **same row** —
        Smartsheet's cellimages endpoint rejects concurrent same-row writes
        with a spurious ``1004`` and the image would be dropped.
        """
        start_time = time.time()
        original_size = 0
        processed_size = 0

        if self._attachment_service is None:
            raise RuntimeError(
                "ImageHandler was constructed without an organization_id; "
                "process() requires a real AttachmentService. Pass the "
                "organization_id to the RowProcessor / ImageHandler "
                "constructor."
            )

        try:
            log.debug(
                f"Downloading attachment {attachment.id}: {attachment.name}"
            )
            download = await self._attachment_service.download(
                sheet_id, attachment
            )
            download_data = download.data
            original_size = download.size_bytes

            # Convert/optimize. CPU-bound (PIL / PyMuPDF) → dispatch to a
            # thread so the event loop stays responsive while we encode.
            log.debug(f"Converting image {attachment.name}")
            processed_data = await asyncio.to_thread(
                self._converter.process,
                download_data,
                attachment.mime_type,
                attachment.name,
            )
            processed_size = len(processed_data)

            # Create temp file (disk write — also off-thread).
            temp_path = await asyncio.to_thread(
                temp_manager.create_temp_file,
                processed_data,
                self._get_output_filename(attachment.name),
            )

            # Upload — direct await, no bridge. Serialized per row (SH-176) so
            # two cellimages writes to the same row never overlap.
            log.debug(f"Uploading to cell {row_id}/{column_id}")
            if upload_lock is not None:
                async with upload_lock:
                    success = await self._attachment_service.upload_to_cell(
                        sheet_id,
                        row_id,
                        column_id,
                        temp_path,
                        mime_type="image/jpeg",
                    )
            else:
                success = await self._attachment_service.upload_to_cell(
                    sheet_id,
                    row_id,
                    column_id,
                    temp_path,
                    mime_type="image/jpeg",
                )

            processing_time = int((time.time() - start_time) * 1000)

            if success:
                log.info(
                    f"Processed {attachment.name}: "
                    f"{original_size} -> {processed_size} bytes "
                    f"({processing_time}ms)"
                )

            return ImageResult(
                success=success,
                attachment_id=attachment.id,
                filename=attachment.name,
                column_id=column_id,
                original_size_bytes=original_size,
                processed_size_bytes=processed_size,
                processing_time_ms=processing_time,
            )

        except ImageProcessingError as e:
            log.warning(f"Image processing failed for {attachment.name}: {e}")
            return ImageResult(
                success=False,
                attachment_id=attachment.id,
                filename=attachment.name,
                column_id=column_id,
                original_size_bytes=original_size,
                processed_size_bytes=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=str(e),
                error_type="ImageProcessingError",
            )

        except Exception as e:
            log.error(f"Failed to process {attachment.name}: {e}")
            return ImageResult(
                success=False,
                attachment_id=attachment.id,
                filename=attachment.name,
                column_id=column_id,
                original_size_bytes=original_size,
                processed_size_bytes=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=str(e),
                error_type=type(e).__name__,
            )

    def _get_output_filename(self, original_name: str) -> str:
        """Get the output filename (always .jpg)."""
        import os
        base = os.path.splitext(original_name)[0]
        return f"{base}.jpg"

    async def process_batch(
        self,
        attachments: list,
        sheet_id: int,
        row_id: int,
        column_mapping: dict,
        temp_manager: TempFileManager,
        parallel: bool = True,
    ) -> list:
        """Process a batch of images, optionally in parallel.

        SH-101: ``parallel=True`` uses ``asyncio.gather`` (capped by a
        per-call semaphore) instead of a thread pool. The image processing
        itself is CPU-bound and goes through ``asyncio.to_thread`` inside
        ``process``, so the asyncio path retains the actual parallelism.
        """
        if not attachments:
            return []

        if parallel and len(attachments) > 1:
            return await self._process_parallel(
                attachments,
                sheet_id,
                row_id,
                column_mapping,
                temp_manager,
            )

        return await self._process_sequential(
            attachments,
            sheet_id,
            row_id,
            column_mapping,
            temp_manager,
        )

    async def _process_sequential(
        self,
        attachments: list,
        sheet_id: int,
        row_id: int,
        column_mapping: dict,
        temp_manager: TempFileManager,
    ) -> list:
        """Process images sequentially (async)."""
        results = []
        for idx, att in enumerate(attachments):
            column_id = column_mapping.get(idx)
            if column_id is None:
                continue
            results.append(
                await self.process(att, sheet_id, row_id, column_id, temp_manager)
            )
        return results

    async def _process_parallel(
        self,
        attachments: list,
        sheet_id: int,
        row_id: int,
        column_mapping: dict,
        temp_manager: TempFileManager,
    ) -> list:
        """Process images in parallel via ``asyncio.gather`` + semaphore."""
        max_workers = min(len(attachments), self._settings.max_parallel_images)
        log.debug(
            f"Processing {len(attachments)} images in parallel "
            f"(workers={max_workers})"
        )

        sem = asyncio.Semaphore(max_workers)
        # SH-176: all attachments in a batch target the same row, so a single
        # lock serializes every cellimages upload for that row while download +
        # conversion still run in parallel under the semaphore.
        upload_lock = asyncio.Lock()
        results: list = [None] * len(attachments)

        async def _one(idx: int, att: Attachment, column_id: int) -> None:
            async with sem:
                try:
                    results[idx] = await self.process(
                        att, sheet_id, row_id, column_id, temp_manager, upload_lock
                    )
                except Exception as e:
                    log.error(f"Parallel image processing failed: {e}")
                    results[idx] = ImageResult(
                        success=False,
                        attachment_id=att.id,
                        filename=att.name,
                        column_id=column_id,
                        original_size_bytes=0,
                        processed_size_bytes=0,
                        processing_time_ms=0,
                        error=str(e),
                        error_type=type(e).__name__,
                    )

        tasks = []
        for idx, att in enumerate(attachments):
            column_id = column_mapping.get(idx)
            if column_id is None:
                continue
            tasks.append(_one(idx, att, column_id))

        await asyncio.gather(*tasks)

        # Filter out None placeholders for the slots that had no column mapping.
        return [r for r in results if r is not None]
