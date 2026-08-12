"""Unit tests for SH-148 distributed backfill lock."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.backfill_lock import (
    BACKFILL_ALREADY_RUNNING,
    hold_worksheet_backfill_lock,
    worksheet_lock_key,
)


def _redis_double(*, acquire: bool = True, extend: bool = True, release: bool = True):
    client = MagicMock(name="RedisClient")
    client.acquire_lock = AsyncMock(return_value=acquire)
    client.extend_lock = AsyncMock(return_value=extend)
    client.release_lock = AsyncMock(return_value=release)
    return client


class TestWorksheetLockKey:
    def test_key_format(self):
        assert worksheet_lock_key(12345) == "lock:backfill:worksheet:12345"


class TestHoldWorksheetBackfillLock:
    @pytest.mark.asyncio
    async def test_no_redis_proceeds_with_warning(self, caplog):
        with patch("src.services.backfill_lock.get_redis_client", return_value=None):
            async with hold_worksheet_backfill_lock(99) as acquired:
                assert acquired is True

        assert "skipping backfill lock" in caplog.text

    @pytest.mark.asyncio
    async def test_busy_lock_yields_false_without_release(self):
        client = _redis_double(acquire=False)
        with patch("src.services.backfill_lock.get_redis_client", return_value=client):
            async with hold_worksheet_backfill_lock(42) as acquired:
                assert acquired is False

        client.release_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_acquires_and_releases(self):
        client = _redis_double()
        with patch("src.services.backfill_lock.get_redis_client", return_value=client):
            async with hold_worksheet_backfill_lock(7, ttl_s=120, heartbeat_interval_s=1) as acquired:
                assert acquired is True
                await asyncio.sleep(0.05)

        client.acquire_lock.assert_awaited_once()
        lock_name, token, ttl = client.acquire_lock.await_args.args
        assert lock_name == worksheet_lock_key(7)
        assert ttl == 120
        assert token
        client.release_lock.assert_awaited_once_with(lock_name, token)

    @pytest.mark.asyncio
    async def test_heartbeat_extends_lock_while_running(self):
        client = _redis_double()
        with patch("src.services.backfill_lock.get_redis_client", return_value=client):
            async with hold_worksheet_backfill_lock(
                5, ttl_s=120, heartbeat_interval_s=0.05
            ) as acquired:
                assert acquired is True
                await asyncio.sleep(0.12)

        assert client.extend_lock.await_count >= 1

    @pytest.mark.asyncio
    async def test_second_acquire_while_first_holds_is_rejected(self):
        """Simulates two workers: only the first acquire_lock succeeds."""
        state = {"held": False}

        async def acquire_side_effect(lock_name, token, ttl_s):
            if not state["held"]:
                state["held"] = True
                state["token"] = token
                state["lock_name"] = lock_name
                return True
            return False

        client = MagicMock(name="RedisClient")
        client.acquire_lock = AsyncMock(side_effect=acquire_side_effect)
        client.extend_lock = AsyncMock(return_value=True)
        client.release_lock = AsyncMock(return_value=True)

        with patch("src.services.backfill_lock.get_redis_client", return_value=client):
            async with hold_worksheet_backfill_lock(88) as first:
                assert first is True
                async with hold_worksheet_backfill_lock(88) as second:
                    assert second is False

        client.release_lock.assert_awaited_once_with(state["lock_name"], state["token"])


class TestBackfillWorksheetLockIntegration:
    @pytest.mark.asyncio
    async def test_backfill_worksheet_returns_busy_when_lock_held(self, processor):
        cfg = MagicMock()
        cfg.id = "cfg-1"
        cfg.organization_id = "org-1"
        cfg.worksheet_id = 999
        processor._config_repo.get_active_webhook_configs = MagicMock(return_value=[cfg])

        @asynccontextmanager
        async def busy_lock(*_args, **_kwargs):
            yield False

        with patch(
            "src.core.processor.hold_worksheet_backfill_lock",
            busy_lock,
        ):
            result = await processor.backfill_worksheet(999)

        assert result.success is False
        assert result.error == BACKFILL_ALREADY_RUNNING
        assert result.processing_time_ms >= 0
