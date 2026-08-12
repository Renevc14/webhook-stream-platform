"""
Poller Worker
=============

Background worker that polls for changes periodically.
"""

import asyncio
import logging
import time
from typing import Optional, List, Dict

from src.workers.base import BaseWorker
from src.core.processor import ImageProcessor
from src.models.config import ImageToCellConfig, ProcessingMode
from src.models.results import ProcessingResult
from src.services.supabase.config_repo import ConfigRepository
from src.config.settings import Settings


log = logging.getLogger(__name__)


class PollerWorker(BaseWorker):
    """
    Background polling worker.

    Periodically scans sheets for new attachments and processes them.
    Used as a fallback when webhooks are not available.
    """

    def __init__(
        self,
        processor: Optional[ImageProcessor] = None,
        settings: Optional[Settings] = None,
    ):
        """
        Initialize poller worker.

        Args:
            processor: Image processor
            settings: Application settings
        """
        super().__init__(processor, settings)
        self._config_repo = ConfigRepository()
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """
        Start the polling loop.

        Runs continuously until stop() is called.
        """
        self._running = True
        self._stop_event.clear()
        self._log_start()

        while self._running and not self._stop_event.is_set():
            try:
                cycle_start = time.time()

                result = await self.process_once()

                cycle_duration = int((time.time() - cycle_start) * 1000)
                self._log_cycle(cycle_duration, result.get("configs_processed", 0))

            except Exception as e:
                log.error(f"Polling cycle failed: {e}")

            # Wait for next cycle
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.polling_interval_s,
                )
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue polling

        self._log_stop()

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self._stop_event.set()

    async def process_once(self) -> Dict:
        """
        Run a single polling cycle.

        Returns:
            Dict with cycle statistics
        """
        result = {
            "configs_processed": 0,
            "configs_failed": 0,
            "rows_processed": 0,
            "rows_failed": 0,
        }

        try:
            # Get organizations with active polling configs
            org_ids = self._config_repo.get_organizations_with_active_configs(
                processing_mode=ProcessingMode.POLLING
            )

            if not org_ids:
                log.debug("No organizations with active polling configs")
                return result

            log.info(f"Polling cycle: {len(org_ids)} organizations")

            # Process organizations in parallel
            await self._process_organizations_parallel(org_ids, result)

            return result

        except Exception as e:
            log.error(f"Polling cycle failed: {e}")
            raise

    async def _process_organizations_parallel(
        self,
        org_ids: List[str],
        result: Dict,
    ) -> None:
        """Process organizations in parallel (SH-101: async-first)."""
        max_workers = self.settings.max_concurrent_orgs
        sem = asyncio.Semaphore(max_workers)

        async def _one(org_id: str):
            async with sem:
                try:
                    return org_id, await self._process_organization(org_id), None
                except Exception as e:
                    return org_id, None, e

        outcomes = await asyncio.gather(*[_one(o) for o in org_ids])
        for org_id, org_result, err in outcomes:
            if err is not None:
                log.error(f"Failed to process organization {org_id}: {err}")
                result["configs_failed"] += 1
                continue
            result["configs_processed"] += org_result.get("configs_processed", 0)
            result["configs_failed"] += org_result.get("configs_failed", 0)
            result["rows_processed"] += org_result.get("rows_processed", 0)
            result["rows_failed"] += org_result.get("rows_failed", 0)

    async def _process_organization(self, org_id: str) -> Dict:
        """Process all configs for an organization."""
        result = {
            "configs_processed": 0,
            "configs_failed": 0,
            "rows_processed": 0,
            "rows_failed": 0,
        }

        try:
            # Get polling configs for this org
            configs = self._config_repo.get_active_for_organization(
                org_id,
                processing_mode=ProcessingMode.POLLING,
            )

            for config in configs:
                try:
                    config_result = await self.processor.process_config(config)

                    result["configs_processed"] += 1
                    result["rows_processed"] += config_result.rows_processed
                    result["rows_failed"] += config_result.rows_failed

                except Exception as e:
                    log.error(f"Failed to process config {config.id}: {e}")
                    result["configs_failed"] += 1

            return result

        except Exception as e:
            log.error(f"Failed to process organization {org_id}: {e}")
            return result


# Singleton instance
_poller_worker: Optional[PollerWorker] = None


def get_poller_worker() -> PollerWorker:
    """Get the global poller worker singleton."""
    global _poller_worker
    if _poller_worker is None:
        _poller_worker = PollerWorker()
    return _poller_worker
