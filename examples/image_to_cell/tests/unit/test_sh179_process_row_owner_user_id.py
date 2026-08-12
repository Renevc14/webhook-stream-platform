"""SH-179 — process_row must forward config.created_by as owner_user_id.

OAuth-only orgs (no org PAT) resolve credentials via the encrypt-tool
broker keyed by user_id. The live webhook + DLQ path goes through
ImageProcessor.process_row; if that call site omits owner_user_id,
ImageHandler builds a SmartsheetClient without a user, the broker
falls back to a missing PAT, and get_attachment fails with
"No Smartsheet credentials… Profile → Connect" even when Connect is
green. Backfill paths already forwarded created_by — this pins the
live path to the same contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_process_row_forwards_created_by_as_owner_user_id():
    from src.core.processor import ImageProcessor
    from src.models.results import RowResult

    from tests.unit.conftest import make_config

    owner = "18c643ad-b416-40fe-9170-8e87a47716ac"
    config = make_config(images_to_process=1, n_image_columns=1)
    # make_config does not set created_by; pin it explicitly for SH-179.
    config = config.model_copy(update={"created_by": owner})
    row = {"id": 5830930674876292, "attachments": [], "cells": []}

    captured: dict = {}

    class _FakeRowProcessor:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            captured["args"] = args

        async def process(self, *args, **kwargs):
            return RowResult(
                success=True,
                row_id=row["id"],
                images_processed=0,
                images_skipped=0,
                processing_time_ms=1,
            )

    processor = ImageProcessor(supabase=MagicMock(), settings=MagicMock())
    processor._state_repo = MagicMock()

    with patch(
        "src.core.processor.preflight_pause_if_blocked", return_value=None
    ), patch(
        "src.core.processor.RowProcessor", _FakeRowProcessor
    ):
        result = await processor.process_row(config, row)

    assert result.success is True
    assert captured["kwargs"]["organization_id"] == config.organization_id
    assert captured["kwargs"]["owner_user_id"] == owner
