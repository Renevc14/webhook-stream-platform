"""SH-155 local validation — real RedisClient against Docker Redis.

Exercises the actual publish plumbing (no mocked Redis):

  Phase A: direct service publishes → XLEN + payload inspection
  Phase B: RowProcessor.process with 2 successes + 1 failure (Smartsheet
           mocked) → stream:platform-events grows by exactly 2
  Phase C: DLQProcessor.process_batch abandoning an event (retries
           exhausted) → stream:i2c:dlq gets 1 actionable entry

Usage:
  docker compose -f docker-compose.sh149.yml up redis -d
  .\\venv\\Scripts\\python.exe scripts/sh155_stream_validation.py

Only touches the local Docker Redis (streams are DELeted first). Supabase
and Smartsheet are mocked — safe to run anywhere.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "shared", "smarthub_core", "src"
    ),
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

PLATFORM = "stream:platform-events"
DLQ = "stream:i2c:dlq"


async def main() -> int:
    from smarthub_core.db.redis import RedisClient

    from src.services.redis_client import set_redis_client
    from src.services import stream_events

    client = RedisClient(REDIS_URL)
    try:
        await client.start()
        pong = await client.raw.ping()
    except Exception as e:
        print(f"FATAL: cannot reach Redis at {REDIS_URL}: {e}")
        return 1
    if not pong:
        print(f"FATAL: Redis at {REDIS_URL} did not answer PING")
        return 1
    set_redis_client(client)

    await client.raw.delete(PLATFORM, DLQ)
    print(f"Redis OK at {REDIS_URL} — streams reset\n")

    failures = 0

    # ---------------- Phase A: direct publishes ----------------
    print("=== Phase A: direct service publishes ===")
    msg1 = await stream_events.publish_image_processed(
        org_id="org-sh155",
        config_id="cfg-sh155",
        worksheet_id=1111,
        row_id=2222,
        attachment_id=3333,
        column_id=4444,
        filename="phaseA.jpg",
    )
    msg2 = await stream_events.publish_dlq_abandoned(
        org_id="org-sh155",
        worksheet_id=1111,
        config_id="cfg-sh155",
        event_type="attachment_created",
        reason="phase A simulated permanent failure",
        dlq_id="dlq-row-phaseA",
        row_id=2222,
        error_type="SmartsheetError",
        retry_count=3,
    )
    plat_len = await client.raw.xlen(PLATFORM)
    dlq_len = await client.raw.xlen(DLQ)
    ok = msg1 and msg2 and plat_len == 1 and dlq_len == 1
    failures += 0 if ok else 1
    print(f"  platform XLEN={plat_len} (want 1)  dlq XLEN={dlq_len} (want 1)"
          f"  -> {'PASS' if ok else 'FAIL'}")
    for name in (PLATFORM, DLQ):
        entries = await client.raw.xrange(name)
        for mid, payload in entries:
            print(f"  {name} {mid}: {payload}")
    print()

    # ---------------- Phase B: RowProcessor success path ----------------
    print("=== Phase B: RowProcessor.process (2 success + 1 fail) ===")
    from src.core import row_processor as rp_mod
    from src.core.row_processor import RowProcessor
    from src.models.results import ImageResult
    from tests.unit.conftest import make_attachment, make_config

    async def _reserve(org, n):
        return True, 9999

    async def _release(org, n):
        return True

    rp_mod.atomic_reserve_bolts = _reserve
    rp_mod.atomic_release_bolts = _release
    rp_mod.get_remaining_image_capacity = lambda org: (100, None)
    rp_mod.log_usage = lambda **kwargs: "fake-usage-uuid"

    cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
    atts = [make_attachment(id=i, name=f"phaseB-{i}.jpg") for i in (1, 2, 3)]
    row = {"id": 5555, "attachments": atts, "cells": []}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings)
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = atts
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(success=True, attachment_id=1, filename="phaseB-1.jpg",
                        column_id=cfg.image_columns[0]),
            ImageResult(success=True, attachment_id=2, filename="phaseB-2.jpg",
                        column_id=cfg.image_columns[1]),
            ImageResult(success=False, attachment_id=3, filename="phaseB-3.jpg",
                        error="simulated upload failure"),
        ]
    )

    before = await client.raw.xlen(PLATFORM)
    result = await rp.process(cfg, row, is_scan_context=False)
    after = await client.raw.xlen(PLATFORM)

    ok = result.images_processed == 2 and (after - before) == 2
    failures += 0 if ok else 1
    print(f"  images_processed={result.images_processed} (want 2)  "
          f"stream delta={after - before} (want 2)  -> {'PASS' if ok else 'FAIL'}")
    entries = await client.raw.xrange(PLATFORM)
    for mid, payload in entries[-2:]:
        print(f"  {PLATFORM} {mid}: type={payload.get('type')} "
              f"row={payload.get('row_id')} att={payload.get('attachment_id')} "
              f"file={payload.get('filename')}")
    print()

    # ---------------- Phase C: DLQ abandonment ----------------
    print("=== Phase C: DLQProcessor abandonment -> stream:i2c:dlq ===")
    from src.workers.dlq_processor import DLQProcessor
    from src.services.supabase.dlq_repo import FailedEvent

    proc = DLQProcessor.__new__(DLQProcessor)
    proc._batch_size = 20
    proc._dlq_repo = MagicMock(name="DLQRepo")
    proc._config_repo = MagicMock(name="ConfigRepo")
    proc._processor = MagicMock(name="ImageProcessor")

    event = FailedEvent(
        id="dlq-phaseC",
        organization_id="org-sh155",
        worksheet_id=1111,
        config_id="cfg-sh155",
        event_type="attachment_created",
        event_data={"row_id": 6666, "attachment_ids": [7777]},
        error_message="simulated: attachment permanently unreadable",
        error_type="ImageProcessingError",
        retry_count=2,
        max_retries=3,
    )
    proc._dlq_repo.get_pending_events.return_value = [event]
    proc._retry_event = AsyncMock(return_value=False)  # final retry fails

    before = await client.raw.xlen(DLQ)
    batch = await proc.process_batch()
    after = await client.raw.xlen(DLQ)

    ok = batch.events_abandoned == 1 and (after - before) == 1
    failures += 0 if ok else 1
    print(f"  events_abandoned={batch.events_abandoned} (want 1)  "
          f"stream delta={after - before} (want 1)  -> {'PASS' if ok else 'FAIL'}")
    entries = await client.raw.xrange(DLQ)
    mid, payload = entries[-1]
    print(f"  {DLQ} {mid}: {payload}")
    print()

    # ---------------- summary ----------------
    plat_len = await client.raw.xlen(PLATFORM)
    dlq_len = await client.raw.xlen(DLQ)
    print(f"Final: XLEN {PLATFORM}={plat_len}  XLEN {DLQ}={dlq_len}")
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} PHASE(S) FAILED")
    await client.close()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
