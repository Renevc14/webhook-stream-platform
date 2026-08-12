"""
Manual Trigger Worker
=====================

Handles on-demand processing triggered by users.
"""

import logging
import time
from typing import Optional
from datetime import datetime

from src.workers.base import BaseWorker
from src.core.processor import ImageProcessor
from src.models.config import ImageToCellConfig
from src.models.results import ProcessingResult
from src.services.supabase.config_repo import ConfigRepository
from src.services.supabase.client import get_supabase_service
from src.config.settings import Settings
from src.config.constants import TABLE_NAMES


log = logging.getLogger(__name__)


class ManualTriggerWorker(BaseWorker):
    """
    Worker for manually triggered processing.

    Allows users to trigger immediate processing of a configuration
    via the UI (e.g., clicking a "process now" button).
    """

    def __init__(
        self,
        processor: Optional[ImageProcessor] = None,
        settings: Optional[Settings] = None,
    ):
        """
        Initialize manual trigger worker.

        Args:
            processor: Image processor
            settings: Application settings
        """
        super().__init__(processor, settings)
        self._config_repo = ConfigRepository()
        self._db = get_supabase_service()

    async def start(self) -> None:
        """Start is not applicable - manual triggers are on-demand."""
        raise NotImplementedError(
            "ManualTriggerWorker is on-demand. Use trigger() instead."
        )

    async def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        self._log_stop()

    async def process_once(self) -> ProcessingResult:
        """Not applicable for manual trigger."""
        raise NotImplementedError(
            "ManualTriggerWorker requires config_id. Use trigger()."
        )

    async def trigger(
        self,
        config_id: str,
        user_id: Optional[str] = None,
    ) -> ProcessingResult:
        """Trigger processing for a configuration (SH-101 async-first)."""
        self._running = True
        start_time = time.time()

        try:
            # Get configuration
            config = self._config_repo.get_by_id(config_id)
            if not config:
                return ProcessingResult(
                    success=False,
                    config_id=config_id,
                    worksheet_id=0,
                    error="Configuration not found",
                )

            log.info(
                f"Manual trigger for config {config.configuration_name} "
                f"by user {user_id or 'unknown'}"
            )

            # Record trigger
            self._record_trigger(config_id, user_id)

            # Process configuration
            result = await self.processor.process_config(config)

            duration_ms = int((time.time() - start_time) * 1000)
            log.info(
                f"Manual trigger completed: {result.rows_processed} rows processed, "
                f"{result.rows_failed} failed ({duration_ms}ms)"
            )

            return result

        except Exception as e:
            log.error(f"Manual trigger failed for {config_id}: {e}")
            return ProcessingResult(
                success=False,
                config_id=config_id,
                worksheet_id=0,
                error=str(e),
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        finally:
            self._running = False

    # SH-102: the legacy ``trigger_async`` alias is gone — there is only
    # ``trigger`` now and it is async.

    def _record_trigger(
        self,
        config_id: str,
        user_id: Optional[str],
    ) -> None:
        """Record the manual trigger event."""
        try:
            self._db.execute_with_retry(
                lambda: self._db.table(TABLE_NAMES.MANUAL_TRIGGERS).insert({
                    "config_id": config_id,
                    "triggered_by": user_id,
                    "triggered_at": datetime.utcnow().isoformat(),
                }).execute(),
                "record manual trigger"
            )
        except Exception as e:
            log.warning(f"Failed to record manual trigger: {e}")


# Singleton instance
_manual_trigger_worker: Optional[ManualTriggerWorker] = None


def get_manual_trigger_worker() -> ManualTriggerWorker:
    """Get the global manual trigger worker singleton."""
    global _manual_trigger_worker
    if _manual_trigger_worker is None:
        _manual_trigger_worker = ManualTriggerWorker()
    return _manual_trigger_worker
