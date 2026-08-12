"""Create-config backfill must be scoped to the new config only.

Regression: create used ``run_for_worksheet``, which loaded every active
webhook config on the sheet and backfilled siblings. Create must queue
``run_for_single_config`` / ``backfill_single_config`` instead. Worksheet-wide
``POST /configs/backfill`` remains for ops.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from tests.unit.conftest import make_config


ORG = "11111111-1111-1111-1111-111111111111"
USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WORKSHEET_ID = 7000000000000001


class _FakeBackfillWorker:
    """Records which backfill entrypoint create queued."""

    def __init__(self) -> None:
        self.single_config_ids: List[str] = []
        self.worksheet_ids: List[int] = []

    async def run_for_single_config(self, config, max_pages=None):
        self.single_config_ids.append(config.id)

    async def run_for_worksheet(self, worksheet_id: int, max_pages=None):
        self.worksheet_ids.append(worksheet_id)


class _fake_lock_acquired:
    """Async context manager that yields True (lock acquired)."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return True

    async def __aexit__(self, *exc):
        return False


def _created_row(*, config_id: str) -> Dict[str, Any]:
    return {
        "id": config_id,
        "organization_id": ORG,
        "worksheet_id": WORKSHEET_ID,
        "configuration_name": "new config",
        "processing_mode": "webhook",
        "config_type": "default",
        "images_to_process": 1,
        "image1_column_id": 8100000000000001,
        "paused": False,
        "created_by": USER,
        "runner_user_id": USER,
    }


@pytest.mark.asyncio
async def test_create_config_queues_single_config_backfill_not_worksheet():
    """POST create must queue run_for_single_config with the new id only."""
    from src.api.routes.configs import create_config
    from src.models.config import ConfigCreate, ProcessingMode, ConfigType

    created = _created_row(config_id="cfg-new-only")
    db = MagicMock()
    db.execute_with_retry.side_effect = lambda fn, _desc: MagicMock(data=[created])

    fake_worker = _FakeBackfillWorker()
    queued_names: List[str] = []
    queued_config_ids: List[Optional[str]] = []

    def _capture_task(coro):
        queued_names.append(coro.cr_code.co_name)
        frame = coro.cr_frame
        if frame is not None:
            cfg = frame.f_locals.get("config")
            queued_config_ids.append(getattr(cfg, "id", None) if cfg else None)
        else:
            queued_config_ids.append(None)
        coro.close()
        return MagicMock()

    body = ConfigCreate(
        organization_id=ORG,
        worksheet_id=WORKSHEET_ID,
        configuration_name="new config",
        processing_mode=ProcessingMode.WEBHOOK,
        config_type=ConfigType.DEFAULT,
        images_to_process=1,
        image_columns=[8100000000000001],
        created_by=USER,
    )

    with patch(
        "src.api.routes.configs.asyncio.create_task",
        side_effect=_capture_task,
    ), patch(
        "src.api.routes.configs.get_backfill_worker",
        return_value=fake_worker,
    ), patch(
        "src.api.routes.configs._register_webhook_for_config",
        new_callable=AsyncMock,
    ):
        result = await create_config(
            config=body,
            background_tasks=BackgroundTasks(),
            org_id=ORG,
            user_id=USER,
            db=db,
        )

    assert result["id"] == "cfg-new-only"
    assert "run_for_single_config" in queued_names
    assert "run_for_worksheet" not in queued_names
    assert "cfg-new-only" in queued_config_ids


@pytest.mark.asyncio
async def test_backfill_single_config_does_not_load_sibling_configs(monkeypatch):
    """Processor path never calls get_active_webhook_configs."""
    from src.core.processor import ImageProcessor
    from src.models.results import BackfillResult

    new_cfg = make_config(config_id="cfg-alone", organization_id=ORG)
    new_cfg.created_by = USER
    new_cfg.runner_user_id = USER

    sibling = make_config(config_id="cfg-sibling", organization_id=ORG)

    proc = ImageProcessor(supabase=MagicMock())
    proc._config_repo = MagicMock()
    proc._config_repo.get_active_webhook_configs.return_value = [sibling, new_cfg]

    monkeypatch.setattr(
        "src.core.processor.preflight_pause_if_blocked",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "src.core.processor.preflight_backfill_budget",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "src.core.processor.hold_worksheet_backfill_lock",
        _fake_lock_acquired,
    )

    sheet_service = MagicMock()
    sheet_service.get_sheet = AsyncMock(return_value={"rows": [{"id": 1}]})
    sheet_service.get_rows_with_attachments = AsyncMock(
        return_value=[{"id": 1, "attachments": []}]
    )
    proc._get_sheet_service = MagicMock(return_value=sheet_service)

    seen_ids: List[str] = []

    async def fake_backfill_config(config, rows, ss, sheet_service=None, **kwargs):
        seen_ids.append(config.id)
        return BackfillResult(worksheet_id=config.worksheet_id, success=True)

    monkeypatch.setattr(proc, "_backfill_config", fake_backfill_config)

    result = await proc.backfill_single_config(new_cfg)

    assert result.success is True
    assert seen_ids == ["cfg-alone"]
    proc._config_repo.get_active_webhook_configs.assert_not_called()
    # Runner identity (not session user) for create-time Smartsheet access.
    proc._get_sheet_service.assert_called_once_with(ORG, USER)


@pytest.mark.asyncio
async def test_worker_run_for_single_config_delegates_to_processor():
    from src.workers.backfill import BackfillWorker
    from src.models.results import BackfillResult

    cfg = make_config(config_id="cfg-w", organization_id=ORG)
    processor = MagicMock()
    processor.backfill_single_config = AsyncMock(
        return_value=BackfillResult(worksheet_id=WORKSHEET_ID, success=True)
    )
    processor.backfill_worksheet = AsyncMock()

    worker = BackfillWorker(
        processor=processor,
        settings=MagicMock(backfill_max_pages=10),
    )
    await worker.run_for_single_config(cfg)

    processor.backfill_single_config.assert_awaited_once()
    assert processor.backfill_single_config.await_args.args[0].id == "cfg-w"
    processor.backfill_worksheet.assert_not_called()
