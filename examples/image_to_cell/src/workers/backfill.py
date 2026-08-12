"""
Backfill Worker
===============

Handles initial backfill when a webhook is registered.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from src.workers.base import BaseWorker
from src.core.processor import ImageProcessor
from src.models.config import ImageToCellConfig
from src.models.results import BackfillResult
from src.services.backfill_lock import BackfillLease
from src.config.settings import Settings


log = logging.getLogger(__name__)


class BackfillWorker(BaseWorker):
    """
    Worker for initial attachment backfill.

    When a webhook is registered for a worksheet that already has
    attachments, this worker processes those existing attachments.

    This is typically run once when the webhook is first registered,
    not as a continuous background service.
    """

    def __init__(
        self,
        processor: Optional[ImageProcessor] = None,
        settings: Optional[Settings] = None,
    ):
        """
        Initialize backfill worker.

        Args:
            processor: Image processor
            settings: Application settings
        """
        super().__init__(processor, settings)

    async def start(self) -> None:
        """
        Start is not applicable for backfill worker.

        Backfill is run on-demand, not as a continuous service.
        """
        raise NotImplementedError(
            "BackfillWorker is on-demand. Use run_for_worksheet() instead."
        )

    async def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        self._log_stop()

    async def process_once(self) -> BackfillResult:
        """
        Not applicable for backfill.

        Use run_for_worksheet() instead.
        """
        raise NotImplementedError(
            "BackfillWorker requires worksheet_id. Use run_for_worksheet()."
        )

    async def run_for_worksheet(
        self,
        worksheet_id: int,
        max_pages: Optional[int] = None,
    ) -> BackfillResult:
        """Run backfill for a specific worksheet (SH-101 async)."""
        self._running = True
        start_time = time.time()

        try:
            log.info(f"Starting backfill for worksheet {worksheet_id}")

            result = await self.processor.backfill_worksheet(
                worksheet_id,
                max_pages=max_pages or self.settings.backfill_max_pages,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            log.info(
                f"Backfill completed for worksheet {worksheet_id}: "
                f"{result.rows_processed} processed, {result.rows_skipped} skipped, "
                f"{result.rows_failed} failed ({duration_ms}ms)"
            )

            return result

        except Exception as e:
            log.error(f"Backfill failed for worksheet {worksheet_id}: {e}")
            return BackfillResult(
                success=False,
                worksheet_id=worksheet_id,
                error=str(e),
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        finally:
            self._running = False

    # SH-102: the legacy ``run_for_worksheet_async`` alias is gone — there
    # is only ``run_for_worksheet`` now and it is async.

    async def run_for_single_config(
        self,
        config: ImageToCellConfig,
        max_pages: Optional[int] = None,
    ) -> BackfillResult:
        """Run create-time backfill for exactly one config.

        Background path (no HTTP estimate / 202). Fetches the sheet as the
        config runner, acquires the worksheet lease internally, and never
        touches sibling configs. Prefer this over :meth:`run_for_worksheet`
        when only the newly created config should be filled.
        """
        self._running = True
        start_time = time.time()

        try:
            log.info(
                "Starting create-time backfill config=%s worksheet=%s",
                config.id,
                config.worksheet_id,
            )

            result = await self.processor.backfill_single_config(
                config,
                max_pages=max_pages or self.settings.backfill_max_pages,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            log.info(
                "Create-time backfill config=%s: %s processed, %s skipped, "
                "%s failed (%sms)",
                config.id,
                result.rows_processed,
                result.rows_skipped,
                result.rows_failed,
                duration_ms,
            )
            return result

        except Exception as e:
            log.error(
                "Create-time backfill failed config=%s: %s",
                config.id,
                e,
            )
            return BackfillResult(
                success=False,
                worksheet_id=config.worksheet_id,
                error=str(e),
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        finally:
            self._running = False

    async def run_for_config(
        self,
        config: ImageToCellConfig,
        rows_with_attachments: List[Dict[str, Any]],
        sheet_service: Any,
        *,
        lease: Optional[BackfillLease] = None,
        session_user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> BackfillResult:
        """Run a backfill for exactly one config (SH-183 Run Backfill).

        The HTTP route has already validated, fetched the rows as the
        session user, revalidated the budget, and secured ``lease``; this
        worker just executes and reports. The pre-acquired lease is
        heartbeat-extended and released by the processor — never
        re-acquired here.
        """
        self._running = True
        start_time = time.time()

        try:
            log.info(
                "Starting config-scoped backfill run=%s config=%s worksheet=%s",
                run_id,
                config.id,
                config.worksheet_id,
            )

            result = await self.processor.backfill_config_prefetched(
                config,
                rows_with_attachments,
                sheet_service,
                lease=lease,
                session_user_id=session_user_id,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            log.info(
                "Config-scoped backfill run=%s config=%s: %s processed, "
                "%s skipped, %s failed (%sms)",
                run_id,
                config.id,
                result.rows_processed,
                result.rows_skipped,
                result.rows_failed,
                duration_ms,
            )
            return result

        except Exception as e:
            log.error(
                "Config-scoped backfill failed run=%s config=%s: %s",
                run_id,
                config.id,
                e,
            )
            return BackfillResult(
                success=False,
                worksheet_id=config.worksheet_id,
                error=str(e),
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        finally:
            self._running = False


# Singleton instance
_backfill_worker: Optional[BackfillWorker] = None


def get_backfill_worker() -> BackfillWorker:
    """Get the global backfill worker singleton."""
    global _backfill_worker
    if _backfill_worker is None:
        _backfill_worker = BackfillWorker()
    return _backfill_worker
