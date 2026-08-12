"""SH-181 — refund Redis bolt-cap reserve when image processing fails.

BillingPort.reserve charges Redis before download/upload. Without a
release on failure, DLQ retries of a broken attachment (e.g. SH-179
credential miss) over-consume Redis while imagetocell_usage_log / Hub
UI stay correct — later live work is falsely denied.

Releases must use ``Reservation.bolts`` (exact hold), never reprice via
``n_images * bolts_per_image``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from smarthub_core.billing import MockBilling, Reservation
from src.models.results import ImageResult
from tests.unit.conftest import make_attachment, make_config


def _run(coro):
    return asyncio.run(coro)


class _RecordingBilling(MockBilling):
    """MockBilling that records release kwargs for assertions."""

    def __init__(self, *, bolts_per_unit: int = 15) -> None:
        super().__init__()
        self.bolts_per_unit = bolts_per_unit
        self.releases: list = []
        self.reserves: list = []

    async def reserve(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        operation_id: str | None = None,
    ) -> Reservation:
        self.reserves.append(
            {
                "organization_id": organization_id,
                "amount": amount,
                "operation_id": operation_id,
            }
        )
        bolts = amount * self.bolts_per_unit
        await super().reserve(organization_id, amount, operation_id=operation_id)
        return Reservation(
            allowed=True, bolts=bolts, operation_id=operation_id
        )

    async def release(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        bolts: int | None = None,
        operation_id: str | None = None,
    ) -> None:
        self.releases.append(
            {
                "organization_id": organization_id,
                "amount": amount,
                "bolts": bolts,
                "operation_id": operation_id,
            }
        )
        await super().release(
            organization_id, amount, bolts=bolts, operation_id=operation_id
        )


def test_process_releases_exact_bolts_for_failed_images():
    from src.core.row_processor import RowProcessor

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=28, name="horse-28.jpg")
    row = {"id": 999, "attachments": [att], "cells": []}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att]
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(
                success=False,
                attachment_id=att.id,
                filename=att.name,
                error="No Smartsheet credentials",
            )
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.core.row_processor.log_usage", lambda **kwargs: "fake-uuid"
        )
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.images_failed == 1
    assert len(billing.reserves) == 1
    assert billing.reserves[0]["amount"] == 1
    assert billing.reserves[0]["operation_id"]
    assert len(billing.releases) == 1
    assert billing.releases[0]["bolts"] == 15
    assert billing.releases[0]["operation_id"] == billing.reserves[0]["operation_id"]
    assert billing.releases[0]["organization_id"] == UUID(str(cfg.organization_id))


def test_process_does_not_release_when_all_images_succeed():
    from src.core.row_processor import RowProcessor

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=1, name="dog.jpg")
    row = {"id": 999, "attachments": [att], "cells": []}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att]
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(
                success=True,
                attachment_id=att.id,
                filename=att.name,
                column_id=cfg.image_columns[0],
                original_size_bytes=1024,
                processing_time_ms=50,
            )
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.core.row_processor.log_usage", lambda **kwargs: "fake-uuid"
        )
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.success is True
    assert billing.releases == []


def test_process_keeps_bolts_on_duplicate_usage_log_after_successful_write():
    """ALREADY_LOGGED after a successful upload must NOT refund Redis.

    Product bills every processed image (reprocess after clear included).
    Refunding on the legacy unique-index sentinel made re-backfill net $0.
    """
    from src.core.row_processor import RowProcessor
    from src.services.usage_log import ALREADY_LOGGED

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=42, name="dup.jpg")
    row = {"id": 999, "attachments": [att], "cells": []}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att]
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(
                success=True,
                attachment_id=att.id,
                filename=att.name,
                column_id=cfg.image_columns[0],
                original_size_bytes=1024,
                processing_time_ms=50,
            )
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.core.row_processor.log_usage",
            lambda **kwargs: ALREADY_LOGGED,
        )
        mp.setattr(
            "src.core.row_processor.publish_image_processed",
            AsyncMock(),
        )
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.success is True
    assert result.images_processed == 1
    assert len(billing.reserves) == 1
    assert billing.releases == []


def test_process_does_not_reserve_when_row_is_skipped():
    """should_process_row false → no reserve / no charge."""
    from src.core.row_processor import RowProcessor
    from src.models.row_state import RowState
    from tests.unit.conftest import make_cell

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="default", n_image_columns=1, images_to_process=1)
    att = make_attachment(id=7, name="skip.jpg")
    # Cell still filled so SH-108 prune does not unlock the row.
    row = {
        "id": 999,
        "attachments": [att],
        "cells": [make_cell(column_id=cfg.image_columns[0], image_alt="skip.jpg")],
    }

    state = RowState(config_id=cfg.id, row_id=999)
    state.last_ids = {att.signature}
    state.last_ts = att.created_timestamp
    state.lock_column(0)

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = state
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att]
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock()

    logged: list = []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.core.row_processor.log_usage",
            lambda **kwargs: logged.append(kwargs) or "fake-uuid",
        )
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.success is True
    assert result.images_processed == 0
    assert billing.reserves == []
    assert billing.releases == []
    assert logged == []
    rp._image_handler.process_batch.assert_not_called()


def test_process_releases_bolts_on_sh111_shuffle_no_bill():
    """SH-111 shuffle: successful relocate of an already-billed attachment
    must refund the Redis hold (no new usage_log).
    """
    from src.core.row_processor import RowProcessor
    from src.models.row_state import RowState

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="replace", n_image_columns=2, images_to_process=2)
    att_old = make_attachment(id=1, name="old.jpg")
    att_new = make_attachment(id=2, name="new.jpg")
    row = {"id": 999, "attachments": [att_old, att_new], "cells": []}

    state = RowState(config_id=cfg.id, row_id=999)
    # Old attachment already billed on a prior run — this run shuffles it.
    state.last_ids = {att_old.signature}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = state
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att_old, att_new]
    rp._image_handler = MagicMock()
    # Replace filter returns newest-first: new then old.
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(
                success=True,
                attachment_id=att_new.id,
                filename=att_new.name,
                column_id=cfg.image_columns[0],
                original_size_bytes=512,
                processing_time_ms=10,
            ),
            ImageResult(
                success=True,
                attachment_id=att_old.id,
                filename=att_old.name,
                column_id=cfg.image_columns[1],
                original_size_bytes=512,
                processing_time_ms=10,
            ),
        ]
    )

    logged: list = []

    def _log(**kwargs):
        logged.append(kwargs)
        return "fake-uuid"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.core.row_processor.log_usage", _log)
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.success is True
    # Only the new attachment is billed; shuffle skips log_usage.
    assert len(logged) == 1
    assert logged[0]["image_filename"] == "new.jpg"
    assert len(billing.reserves) == 1
    assert billing.reserves[0]["amount"] == 2
    # One of two reserved images was shuffle_no_bill → refund half.
    assert len(billing.releases) == 1
    assert billing.releases[0]["bolts"] == 15
    assert billing.releases[0]["amount"] == 1
    assert billing.releases[0]["operation_id"] == billing.reserves[0]["operation_id"]


def test_process_partial_refund_keeps_all_successful_writes():
    """Mixed batch: one new bill + one ALREADY_LOGGED success → no refund.

    Both images were written; both keep their Redis hold under the
    charge-on-process policy.
    """
    from src.core.row_processor import RowProcessor
    from src.services.usage_log import ALREADY_LOGGED

    billing = _RecordingBilling(bolts_per_unit=15)

    cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
    att1 = make_attachment(id=1, name="a.jpg")
    att2 = make_attachment(id=2, name="b.jpg")
    row = {"id": 999, "attachments": [att1, att2], "cells": []}

    settings = MagicMock(name="Settings")
    settings.enable_parallel_image_processing = False
    rp = RowProcessor(MagicMock(), MagicMock(), settings, billing=billing)
    rp._state_repo.get.return_value = None
    rp._sheet_service = MagicMock()
    rp._sheet_service.extract_row_attachments.return_value = [att1, att2]
    rp._image_handler = MagicMock()
    rp._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(
                success=True,
                attachment_id=att1.id,
                filename=att1.name,
                column_id=cfg.image_columns[0],
                original_size_bytes=100,
                processing_time_ms=10,
            ),
            ImageResult(
                success=True,
                attachment_id=att2.id,
                filename=att2.name,
                column_id=cfg.image_columns[1],
                original_size_bytes=100,
                processing_time_ms=10,
            ),
        ]
    )

    def _log(**kwargs):
        if kwargs.get("image_filename") == "b.jpg":
            return ALREADY_LOGGED
        return "new-uuid"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.core.row_processor.log_usage", _log)
        mp.setattr(
            "src.core.row_processor.publish_image_processed",
            AsyncMock(),
        )
        result = _run(rp.process(cfg, row, is_scan_context=False))

    assert result.images_processed == 2
    assert billing.reserves[0]["amount"] == 2
    assert billing.releases == []


@pytest.mark.asyncio
async def test_atomic_release_bolts_calls_redis_release():
    """Legacy helper still refunds via Redis for callers not on BillingPort."""
    from unittest.mock import patch

    from src.services import bolt_cap

    org = "78e2278d-ffaa-4299-a969-9424275cc207"
    redis = MagicMock()
    redis.atomic_release = AsyncMock(return_value=180)

    with patch(
        "src.services.redis_client.get_redis_client", return_value=redis
    ), patch(
        "src.services.bolt_cap.load_i2c_org_usage_context_async",
        new=AsyncMock(return_value={"bolts_per_image": 15}),
    ):
        ok = await bolt_cap.atomic_release_bolts(org, n_images=4)

    assert ok is True
    redis.atomic_release.assert_awaited_once_with(
        f"bolt-cap:{org}:consumed",
        60,
    )


@pytest.mark.asyncio
async def test_atomic_reserve_denies_when_no_redis_outside_local(monkeypatch):
    from src.services import bolt_cap

    org = "78e2278d-ffaa-4299-a969-9424275cc207"
    settings = MagicMock()
    settings.is_local = False

    monkeypatch.setattr(
        "src.services.redis_client.get_redis_client", lambda: None
    )
    monkeypatch.setattr(
        "src.services.bolt_cap.load_i2c_org_usage_context_async",
        AsyncMock(
            return_value={
                "bolts_per_image": 15,
                "ceiling_bolts": 1000,
                "consumed_bolts_start": 0,
            }
        ),
    )
    monkeypatch.setattr(
        "src.config.settings.get_settings", lambda: settings
    )

    allowed, headroom = await bolt_cap.atomic_reserve_bolts(org, n_images=1)
    assert allowed is False
    assert headroom == 0
