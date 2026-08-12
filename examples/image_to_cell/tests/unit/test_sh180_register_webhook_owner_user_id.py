"""SH-180 — POST /configs/{id}/register-webhook must forward created_by.

The Hub create modal inserts imagetocell_config via Supabase, then calls
register-webhook. That endpoint used to select without created_by, so
owner_user_id was always None. OAuth-only orgs (no org PAT) then failed
create-time registration with "No Smartsheet credentials" even when
Connect was green; lifecycle recovered ~5 minutes later because it does
load created_by.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _close_coro(coro):
    """create_task stand-in: discard the background coroutine in unit tests."""
    coro.close()
    return MagicMock()


def _db_with_config(row: dict) -> MagicMock:
    """Build a db mock that runs execute_with_retry lambdas against a chain."""
    db = MagicMock()
    result = MagicMock(data=[row])
    (
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute
    ).return_value = result
    db.execute_with_retry.side_effect = lambda fn, _desc: fn()
    return db


@pytest.mark.asyncio
async def test_register_webhook_forwards_created_by_as_owner_user_id():
    from src.api.routes.configs import register_webhook_for_config

    owner = "18c643ad-b416-40fe-9170-8e87a47716ac"
    org_id = "78e2278d-ffaa-4299-a969-9424275cc207"
    config_id = "0c85a414-6c4a-423b-bb67-96609f7ad11d"
    worksheet_id = 1124052763627396

    db = _db_with_config(
        {
            "worksheet_id": worksheet_id,
            "configuration_name": "free1",
            "processing_mode": "webhook",
            "paused": False,
            "created_by": owner,
        }
    )

    mock_register = AsyncMock(return_value=True)

    with patch(
        "src.api.routes.configs.asyncio.create_task",
        side_effect=_close_coro,
    ), patch(
        "src.api.routes.configs._register_webhook_for_config",
        mock_register,
    ):
        result = await register_webhook_for_config(
            config_id=config_id,
            org_id=org_id,
            user_id="header-fallback-should-not-win",
            db=db,
        )

    assert result == {"status": "started", "config_id": config_id}
    # Endpoint builds the coroutine then hands it to create_task — the
    # AsyncMock call itself is sync, so assert_called (not awaited).
    mock_register.assert_called_once_with(
        worksheet_id=worksheet_id,
        org_id=org_id,
        config_name="free1",
        owner_user_id=owner,
    )

    select_arg = db.table.return_value.select.call_args.args[0]
    assert "created_by" in select_arg


@pytest.mark.asyncio
async def test_register_webhook_falls_back_to_request_user_id():
    from src.api.routes.configs import register_webhook_for_config

    org_id = "78e2278d-ffaa-4299-a969-9424275cc207"
    header_user = "18c643ad-b416-40fe-9170-8e87a47716ac"

    db = _db_with_config(
        {
            "worksheet_id": 1124052763627396,
            "configuration_name": "free1",
            "processing_mode": "webhook",
            "paused": False,
            "created_by": None,
        }
    )

    mock_register = AsyncMock(return_value=True)

    with patch(
        "src.api.routes.configs.asyncio.create_task",
        side_effect=_close_coro,
    ), patch(
        "src.api.routes.configs._register_webhook_for_config",
        mock_register,
    ):
        result = await register_webhook_for_config(
            config_id="cfg-1",
            org_id=org_id,
            user_id=header_user,
            db=db,
        )

    assert result["status"] == "started"
    assert mock_register.call_args.kwargs["owner_user_id"] == header_user
