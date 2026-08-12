"""SH-176 — name_map bounded retries and partial-failure state tracking."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.modes.name_map import NameMapMode
from src.models.results import ImageResult
from src.models.row_state import RowState
from tests.unit.conftest import make_attachment, make_config


def _make_name_map_config():
    # SH-220: name_map matches only namemap_column* CONTAINS patterns
    # (no fuzzy fallback on image*_column_name).
    return make_config(
        config_type="name map",
        n_image_columns=2,
        images_to_process=2,
        column_name_prefix="part",
        namemap_patterns={1: "part-1", 2: "part-2"},
    )


def test_pending_skips_successful_signatures():
    cfg = _make_name_map_config()
    a1 = make_attachment(id=1, name="part-1.jpg")
    a2 = make_attachment(id=2, name="part-2.jpg")

    state = RowState(config_id=cfg.id, row_id=80)
    state.last_ids.add(a1.signature)

    pending = NameMapMode(max_upload_attempts=5).get_pending_attachments(
        [a1, a2], state, cfg
    )

    assert pending == [a2]


def test_pending_retries_failed_attachment_after_sibling_success():
    """Partial failure must not orphan the failed column (Beth incident)."""
    cfg = _make_name_map_config()
    equipment = make_attachment(id=10, name="part-1.jpg")
    barcode = make_attachment(id=20, name="part-2.jpg")

    state = RowState(config_id=cfg.id, row_id=3409233803673476)
    state.last_ids.add(barcode.signature)
    state.last_ts = barcode.created_timestamp

    pending = NameMapMode(max_upload_attempts=5).get_pending_attachments(
        [equipment, barcode], state, cfg
    )

    assert pending == [equipment]


def test_pending_stops_after_max_attempts():
    cfg = _make_name_map_config()
    att = make_attachment(id=1, name="part-1.jpg")

    state = RowState(config_id=cfg.id, row_id=80)
    state.failed_attempts[att.signature] = 5

    pending = NameMapMode(max_upload_attempts=5).get_pending_attachments(
        [att], state, cfg
    )

    assert pending == []


def test_record_processing_outcomes_tracks_failures_and_clears_on_success():
    cfg = _make_name_map_config()
    mode = NameMapMode(max_upload_attempts=5)
    a_ok = make_attachment(id=1, name="part-1.jpg")
    a_fail = make_attachment(id=2, name="part-2.jpg")

    state = RowState(config_id=cfg.id, row_id=80)
    state.failed_attempts[a_ok.signature] = 2

    results = [
        ImageResult(success=True, attachment_id=a_ok.id, filename=a_ok.name),
        ImageResult(success=False, attachment_id=a_fail.id, filename=a_fail.name, error="1004"),
    ]

    state = mode.record_processing_outcomes(state, [a_ok, a_fail], results)

    assert a_ok.signature not in state.failed_attempts
    assert state.failed_attempts[a_fail.signature] == 1


@pytest.mark.asyncio
async def test_row_processor_saves_state_when_all_uploads_fail():
    from src.core.row_processor import RowProcessor
    from tests.unit.conftest import make_row

    cfg = _make_name_map_config()
    a1 = make_attachment(id=1, name="part-1.jpg")
    a2 = make_attachment(id=2, name="part-2.jpg")
    row = make_row(row_id=80, attachments=[a1, a2])

    state_repo = MagicMock()
    settings = MagicMock()
    settings.enable_parallel_image_processing = False
    settings.name_map_max_upload_attempts = 5

    ss = MagicMock()
    from smarthub_core.billing import MockBilling

    processor = RowProcessor(ss, state_repo, settings, billing=MockBilling())
    processor._sheet_service.extract_row_attachments = MagicMock(return_value=[a1, a2])
    processor._extract_cell_images = MagicMock(return_value={})
    processor._image_handler.process_batch = AsyncMock(
        return_value=[
            ImageResult(success=False, attachment_id=a1.id, filename=a1.name, error="1004"),
            ImageResult(success=False, attachment_id=a2.id, filename=a2.name, error="1004"),
        ]
    )

    with patch("src.core.row_processor.log_usage"):
        result = await processor.process(
            cfg,
            row,
            state=RowState(config_id=cfg.id, row_id=80),
        )

    assert result.images_failed == 2
    state_repo.save.assert_called_once()
    saved_state = state_repo.save.call_args[0][0]
    assert saved_state.failed_attempts[a1.signature] == 1
    assert saved_state.failed_attempts[a2.signature] == 1
    assert not saved_state.last_ids
