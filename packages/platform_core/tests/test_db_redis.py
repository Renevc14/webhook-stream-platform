"""
Integration tests for smarthub_core.db.redis.

Requires a running Redis instance at REDIS_TEST_URL (default redis://127.0.0.1:6379/15).
Tests use DB 15 and FLUSHDB before each test for isolation.

Run from shared/smarthub_core:
    ../.venv/Scripts/python.exe -m pytest tests/test_db_redis.py -v
"""

import asyncio
import uuid

import pytest

from smarthub_core.db.redis import RedisClient, RedisHelperError


# ---------------------------------------------------------------------------
# Lifecycle & health
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_health_check_returns_true_when_connected(self, rc: RedisClient):
        assert await rc.health_check() is True

    async def test_health_check_returns_false_when_not_started(self):
        client = RedisClient("redis://127.0.0.1:6379/15")
        # not started — should not crash, just return False
        assert await client.health_check() is False

    async def test_raw_property_raises_when_not_started(self):
        client = RedisClient("redis://127.0.0.1:6379/15")
        with pytest.raises(RedisHelperError, match="not started"):
            _ = client.raw

    async def test_context_manager(self):
        async with RedisClient("redis://127.0.0.1:6379/15") as cm_rc:
            assert await cm_rc.health_check() is True
            await cm_rc.cache_set("ctx:test", "ok", ttl_s=5)
            assert await cm_rc.cache_get("ctx:test") == "ok"
            await cm_rc.cache_del("ctx:test")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    async def test_get_returns_none_on_miss(self, rc: RedisClient):
        assert await rc.cache_get("absent") is None

    async def test_set_then_get(self, rc: RedisClient):
        assert await rc.cache_set("k1", "v1", ttl_s=10) is True
        assert await rc.cache_get("k1") == "v1"

    async def test_set_respects_ttl(self, rc: RedisClient):
        assert await rc.cache_set("k_ttl", "v", ttl_s=1) is True
        await asyncio.sleep(1.2)
        assert await rc.cache_get("k_ttl") is None

    async def test_del_removes_keys(self, rc: RedisClient):
        await rc.cache_set("d1", "x", ttl_s=10)
        await rc.cache_set("d2", "y", ttl_s=10)
        assert await rc.cache_del("d1", "d2") == 2
        assert await rc.cache_get("d1") is None
        assert await rc.cache_get("d2") is None

    async def test_del_returns_zero_when_no_keys_provided(self, rc: RedisClient):
        assert await rc.cache_del() == 0

    async def test_set_rejects_zero_ttl(self, rc: RedisClient):
        with pytest.raises(RedisHelperError):
            await rc.cache_set("k", "v", ttl_s=0)

    async def test_set_rejects_negative_ttl(self, rc: RedisClient):
        with pytest.raises(RedisHelperError):
            await rc.cache_set("k", "v", ttl_s=-5)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    async def test_first_call_wins_second_loses(self, rc: RedisClient):
        key = "dedup:test:1"
        assert await rc.dedup_set(key, ttl_s=10) is True
        assert await rc.dedup_set(key, ttl_s=10) is False

    async def test_dedup_key_expires_and_can_be_set_again(self, rc: RedisClient):
        key = "dedup:test:expire"
        assert await rc.dedup_set(key, ttl_s=1) is True
        await asyncio.sleep(1.2)
        assert await rc.dedup_set(key, ttl_s=10) is True


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


class TestLocks:
    async def test_acquire_succeeds_when_free(self, rc: RedisClient):
        token = str(uuid.uuid4())
        assert await rc.acquire_lock("lock:a", token, ttl_s=10) is True

    async def test_acquire_fails_when_held(self, rc: RedisClient):
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())
        assert await rc.acquire_lock("lock:b", token_a, ttl_s=10) is True
        assert await rc.acquire_lock("lock:b", token_b, ttl_s=10) is False

    async def test_release_only_works_with_correct_token(self, rc: RedisClient):
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())
        await rc.acquire_lock("lock:c", token_a, ttl_s=10)
        # Wrong token cannot release
        assert await rc.release_lock("lock:c", token_b) is False
        # Correct token can release
        assert await rc.release_lock("lock:c", token_a) is True
        # After release the lock is free
        assert await rc.acquire_lock("lock:c", token_b, ttl_s=10) is True

    async def test_release_returns_false_when_lock_does_not_exist(self, rc: RedisClient):
        assert await rc.release_lock("lock:never-acquired", "any-token") is False

    async def test_extend_only_works_with_correct_token(self, rc: RedisClient):
        token_a = str(uuid.uuid4())
        token_b = str(uuid.uuid4())
        await rc.acquire_lock("lock:d", token_a, ttl_s=2)
        # Wrong token cannot extend
        assert await rc.extend_lock("lock:d", token_b, ttl_s=10) is False
        # Correct token can extend
        assert await rc.extend_lock("lock:d", token_a, ttl_s=10) is True
        # After extension the lock survives the original 2s window
        await asyncio.sleep(2.5)
        # Still held — token_b cannot acquire
        assert await rc.acquire_lock("lock:d", token_b, ttl_s=10) is False

    async def test_lock_expires_naturally(self, rc: RedisClient):
        token = str(uuid.uuid4())
        await rc.acquire_lock("lock:e", token, ttl_s=1)
        await asyncio.sleep(1.2)
        # Now anyone can acquire
        new_token = str(uuid.uuid4())
        assert await rc.acquire_lock("lock:e", new_token, ttl_s=10) is True


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


class TestStreams:
    async def test_publish_returns_id(self, rc: RedisClient):
        msg_id = await rc.stream_publish("stream:t1", {"k": "v"})
        assert msg_id is not None
        assert "-" in msg_id  # Redis stream IDs are "<ms>-<seq>"

    async def test_publish_rejects_empty_payload(self, rc: RedisClient):
        with pytest.raises(RedisHelperError):
            await rc.stream_publish("stream:t1", {})

    async def test_publish_consume_ack_roundtrip(self, rc: RedisClient):
        stream = "stream:t2"
        group = "g1"
        consumer = "c1"

        await rc.ensure_consumer_group(stream, group)
        published_id = await rc.stream_publish(stream, {"event": "hello", "v": "1"})

        received = []
        async for msg_id, payload in rc.stream_consume(
            stream, group, consumer, count=10, block_ms=500
        ):
            received.append((msg_id, payload))
            await rc.stream_ack(stream, group, msg_id)
            break  # only need 1 message for this test

        assert len(received) == 1
        msg_id, payload = received[0]
        assert msg_id == published_id
        assert payload == {"event": "hello", "v": "1"}

    async def test_ensure_consumer_group_is_idempotent(self, rc: RedisClient):
        stream = "stream:t3"
        await rc.ensure_consumer_group(stream, "g1")
        # Calling again must not raise
        await rc.ensure_consumer_group(stream, "g1")

    async def test_pending_redelivery_after_consumer_disconnect(self, rc: RedisClient):
        """If a consumer reads a message but never ACKs, a different consumer in
        the same group can claim it. This is the failure-recovery property of
        Streams that listas simples lack."""
        stream = "stream:t4"
        group = "g1"

        await rc.ensure_consumer_group(stream, group)
        await rc.stream_publish(stream, {"x": "1"})

        # Consumer A reads but does not ack
        first_batch = []
        async for msg_id, payload in rc.stream_consume(
            stream, group, "consumer-a", count=10, block_ms=500
        ):
            first_batch.append(msg_id)
            break
        assert len(first_batch) == 1
        msg_id_a = first_batch[0]

        # Consumer B can claim the pending message via XCLAIM (raw API,
        # min-idle-time = 0). The library helper would expose this in a future
        # version; for now we exercise the raw client.
        claimed = await rc.raw.xclaim(
            stream, group, "consumer-b", min_idle_time=0, message_ids=[msg_id_a]
        )
        assert len(claimed) == 1
        assert claimed[0][0] == msg_id_a

    async def test_dlq_publish_uses_dlq_suffix(self, rc: RedisClient):
        base = "stream:t5"
        dlq_id = await rc.stream_dlq_publish(
            base,
            {"event_id": "abc", "reason": "fatal", "tb": "..."},
        )
        assert dlq_id is not None
        # The DLQ stream is the base + ":dlq"
        info = await rc.raw.xinfo_stream(f"{base}:dlq")
        assert info["length"] == 1


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_under_limit_allows(self, rc: RedisClient):
        for i in range(5):
            allowed, count = await rc.rate_limit_consume(
                "rl:t1", window_s=10, max_calls=10
            )
            assert allowed is True
            assert count == i + 1

    async def test_over_limit_denies(self, rc: RedisClient):
        for _ in range(3):
            allowed, _ = await rc.rate_limit_consume(
                "rl:t2", window_s=10, max_calls=3
            )
            assert allowed is True
        # Next one is over the limit
        allowed, count = await rc.rate_limit_consume(
            "rl:t2", window_s=10, max_calls=3
        )
        assert allowed is False
        assert count == 3

    async def test_sliding_window_forgets_old_entries(self, rc: RedisClient):
        """After the window passes, old entries are evicted and capacity is restored."""
        for _ in range(2):
            allowed, _ = await rc.rate_limit_consume(
                "rl:t3", window_s=1, max_calls=2
            )
            assert allowed is True
        # 3rd call within the same 1s window is denied
        allowed, _ = await rc.rate_limit_consume(
            "rl:t3", window_s=1, max_calls=2
        )
        assert allowed is False
        # Wait past the window
        await asyncio.sleep(1.2)
        # Now we have capacity again
        allowed, count = await rc.rate_limit_consume(
            "rl:t3", window_s=1, max_calls=2
        )
        assert allowed is True
        assert count == 1

    async def test_invalid_args_raise(self, rc: RedisClient):
        with pytest.raises(RedisHelperError):
            await rc.rate_limit_consume("rl:bad", window_s=0, max_calls=10)
        with pytest.raises(RedisHelperError):
            await rc.rate_limit_consume("rl:bad", window_s=10, max_calls=0)


# ---------------------------------------------------------------------------
# Lua script recovery (Redis restart / ElastiCache failover)
# ---------------------------------------------------------------------------


class TestLuaScriptRecovery:
    async def test_rate_limit_survives_script_flush(self, rc: RedisClient):
        """Stale cached SHA1 is reloaded transparently after SCRIPT FLUSH."""
        key = "rl:noscript"
        allowed, count_before = await rc.rate_limit_consume(
            key, window_s=10, max_calls=10
        )
        assert allowed is True
        await rc.raw.script_flush()
        allowed, count_after = await rc.rate_limit_consume(
            key, window_s=10, max_calls=10
        )
        assert allowed is True
        assert count_after == count_before + 1

    async def test_lua_scripts_health_check_true_when_connected(self, rc: RedisClient):
        assert await rc.lua_scripts_health_check() is True

    async def test_lua_scripts_health_check_false_when_not_started(self):
        client = RedisClient("redis://127.0.0.1:6379/15")
        assert await client.lua_scripts_health_check() is False

    async def test_atomic_deduct_survives_script_flush(self, rc: RedisClient):
        consumed = "bolt:test:consumed"
        limit = "bolt:test:limit"
        await rc.raw.set(limit, "100")
        await rc.raw.delete(consumed)
        allowed, remaining = await rc.atomic_deduct(consumed, limit, 5)
        assert allowed is True
        await rc.raw.script_flush()
        allowed, remaining = await rc.atomic_deduct(consumed, limit, 5)
        assert allowed is True
        assert remaining == 90
