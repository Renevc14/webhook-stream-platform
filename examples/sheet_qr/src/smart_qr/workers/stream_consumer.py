"""Start/stop wrapper for the SmartQR stream worker.

SH-212 moved the consumer loop itself to ``SmartQRStreamWorker``
(``smart_qr/workers/stream_worker.py``), which subclasses the shared
``SmarthubWorker``. This module keeps the small lifecycle API the FastAPI app
uses and owns dependency construction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from smart_qr.config.settings import Settings

log = logging.getLogger(__name__)

_WORKER: Optional[Any] = None


async def build_processor() -> Any:
    """Assemble ProcessRowService with its production adapters."""
    from smart_qr.adapters.billing.port import get_billing
    from smart_qr.adapters.db.repositories import (
        ConfigRepository,
        RowStateRepository,
        UsageRepository,
    )
    from smart_qr.adapters.smartsheet.client import get_smartsheet_provider
    from smart_qr.application.process_row import ProcessRowService
    from smart_qr.config.settings import get_settings

    settings = get_settings()
    return ProcessRowService(
        configs=ConfigRepository(),
        row_states=RowStateRepository(),
        usage=UsageRepository(),
        smartsheet=get_smartsheet_provider(settings),
        billing=await get_billing(settings),
        change_agent=settings.change_agent_id,
    )


async def start_stream_consumer(settings: Settings) -> Optional[asyncio.Task]:
    """Start the worker in the background. Returns None when Redis is missing."""
    global _WORKER

    from smart_qr.adapters.core_infra import get_core_redis
    from smart_qr.workers.stream_worker import SmartQRStreamWorker

    try:
        redis = await get_core_redis(settings)
    except Exception as exc:  # noqa: BLE001
        log.error("Stream consumer not started — Redis unavailable: %s", exc)
        return None

    worker = SmartQRStreamWorker(redis)
    _WORKER = worker
    log.info(
        "Stream consumer listening on %s group=%s consumer=%s",
        worker.stream_name,
        worker.group_name,
        worker.consumer_name,
    )

    async def _runner() -> None:
        while not worker.is_shutting_down:
            try:
                await worker.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A Redis drop must not silently kill webhook processing for
                # the rest of the pod's life — reconnect and keep going.
                log.warning("Stream worker crashed, restarting in 2s: %s", exc)
                await asyncio.sleep(2.0)

    return asyncio.create_task(_runner(), name="smart-qr-stream-consumer")


async def stop_stream_consumer(task: Optional[asyncio.Task]) -> None:
    global _WORKER
    if _WORKER is not None:
        _WORKER.request_shutdown()
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _WORKER = None
