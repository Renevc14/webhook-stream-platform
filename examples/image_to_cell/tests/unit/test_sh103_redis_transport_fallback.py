"""SH-103 — bolt_cap falls back when atomic_deduct raises RedisHelperError."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smarthub_core.db.redis import RedisHelperError

from src.services import bolt_cap


@pytest.mark.asyncio
async def test_atomic_reserve_falls_back_on_redis_helper_error():
    """Transport errors must not masquerade as cap deny (remaining=0)."""
    org = "76ed6b48-0b46-4ceb-95d3-91f2b55ee5d3"
    redis_client = MagicMock()
    redis_client.raw = MagicMock()
    redis_client.raw.set = AsyncMock(return_value=True)
    redis_client.raw.delete = AsyncMock(return_value=0)
    redis_client.atomic_deduct_with_cycle = AsyncMock(
        side_effect=RedisHelperError("stale lua sha")
    )

    ctx = {
        "bolts_per_image": 15,
        "ceiling_bolts": 99999,
        "consumed_bolts_start": 0,
        "current_cycle_start": "2026-07-01T00:00:00+00:00",
    }

    with patch("src.services.redis_client.get_redis_client", return_value=redis_client), patch(
        "src.services.bolt_cap.load_i2c_org_usage_context_async",
        new=AsyncMock(return_value=ctx),
    ), patch(
        "src.services.bolt_cap._legacy_check_for_images", return_value=(True, 1000)
    ) as legacy:
        allowed, remaining = await bolt_cap.atomic_reserve_bolts(org, n_images=1)

    assert allowed is True
    legacy.assert_called_once()
