"""Focused unit tests for smarthub_core.billing (no live Redis required)."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import httpx
import pytest

from smarthub_core.billing import (
    DEFAULT_USAGE_RPC,
    REASON_CEILING,
    REASON_INSUFFICIENT,
    REASON_UNKNOWN_BUDGET,
    REASON_UNPRICED,
    REST_ATTEMPTS,
    BoltBilling,
    BoltGate,
    PlatformBudgetReader,
    consumed_key,
    cycle_id_key,
    hold_key,
    limit_key,
    release_bolts,
    reserve_bolts,
    seed_limit,
)
from smarthub_core.db.redis import RedisHelperError

ORG = UUID("11111111-1111-1111-1111-111111111111")
CYCLE = "2026-07-01T00:00:00+00:00"
PRICE = 8
USAGE = 100


class FakeRaw:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    async def set(self, key: str, value: Any, ex: Any = None, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                n += 1
        return n

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)


class FakeRedis:
    """Mirrors RedisClient billing helpers without a server."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.raw = FakeRaw(self.store)
        self.evals = 0

    async def cache_get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def cache_set(self, key: str, value: str, ttl_s: int) -> bool:
        self.store[key] = str(value)
        return True

    async def cache_del(self, *keys: str) -> int:
        return await self.raw.delete(*keys)

    async def atomic_deduct(
        self, consumed: str, limit: str, requested: int
    ) -> tuple[bool, int]:
        self.evals += 1
        cur = int(self.store.get(consumed, "0"))
        raw_limit = self.store.get(limit)
        if raw_limit is None:
            self.store[consumed] = str(cur + requested)
            return True, -1
        cap = int(raw_limit)
        if cur + requested <= cap:
            self.store[consumed] = str(cur + requested)
            return True, cap - (cur + requested)
        return False, cap - cur

    async def atomic_deduct_with_cycle(
        self,
        consumed: str,
        limit: str,
        cycle: str,
        requested: int,
        expected_cycle: str,
        baseline: int,
    ) -> tuple[bool, int]:
        self.evals += 1
        if self.store.get(cycle) != expected_cycle:
            self.store[consumed] = str(baseline)
            self.store[cycle] = expected_cycle
        return await self.atomic_deduct(consumed, limit, requested)

    async def atomic_release(self, consumed: str, amount: int) -> int:
        self.evals += 1
        cur = int(self.store.get(consumed, "0"))
        new = max(0, cur - amount)
        self.store[consumed] = str(new)
        return new


class ExplodingRedis(FakeRedis):
    async def atomic_deduct_with_cycle(self, *a: Any, **k: Any) -> tuple[bool, int]:
        raise RedisHelperError("redis is down")

    async def atomic_deduct(self, *a: Any, **k: Any) -> tuple[bool, int]:
        raise RedisHelperError("redis is down")

    async def atomic_release(self, *a: Any, **k: Any) -> int:
        raise RedisHelperError("redis is down")


class Platform:
    """PostgREST stand-in for subscription + usage + pricing reads."""

    def __init__(
        self,
        *,
        subscription: dict | None = None,
        no_subscription: bool = False,
        plan_suspended: bool = False,
        bolts_pending_payment: bool = False,
        plan_type: str = "team",
        price: int | None = PRICE,
        price_active: bool = True,
        usage_total: int = USAGE,
        fail: str | None = None,
        fail_status: int = 500,
    ) -> None:
        self.subscription = subscription or {}
        self.no_subscription = no_subscription
        self.plan_suspended = plan_suspended
        self.bolts_pending_payment = bolts_pending_payment
        self.plan_type = plan_type
        self.price = price
        self.price_active = price_active
        self.usage_total = usage_total
        self.fail = fail
        self.fail_status = fail_status
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(path)
        if self.fail == path:
            return httpx.Response(self.fail_status, json={"message": "forced"})

        if path == "get_subscription_status":
            if self.no_subscription:
                return httpx.Response(200, json=[])
            row = {
                "total_bolts_allocated": 1000,
                "total_bolts_consumed": 9999,  # must be ignored
                "hard_cap_reached": False,
                "can_use_tools": True,
                "hard_cap_bolt_amount": 0,
                "current_cycle_start": CYCLE,
            }
            row.update(self.subscription)
            return httpx.Response(200, json=[row])
        if path == DEFAULT_USAGE_RPC:
            return httpx.Response(200, json=[{"total_bolts": self.usage_total}])
        if path == "organization_subscriptions":
            return httpx.Response(
                200,
                json=[
                    {
                        "plan_suspended": self.plan_suspended,
                        "bolts_pending_payment": self.bolts_pending_payment,
                    }
                ],
            )
        if path == "organizations":
            return httpx.Response(200, json=[{"plan_type": self.plan_type}])
        if path == "pricing_list":
            if self.price is None:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "primary_event_bolts": self.price,
                        "is_active": self.price_active,
                    }
                ],
            )
        raise AssertionError(f"unexpected billing read: {path}")


def build_gate(
    platform: Platform, redis: Any = None, *, allow_when_unknown: bool = False
) -> BoltGate:
    reader = PlatformBudgetReader(
        supabase_url="https://project.supabase.co",
        service_role_key="service-role-key",
        product_slug="smart-qr",
        usage_rpc=DEFAULT_USAGE_RPC,
    )
    reader._client = httpx.AsyncClient(
        base_url="https://project.supabase.co/rest/v1",
        transport=httpx.MockTransport(platform),
    )
    return BoltGate(
        reader=reader,
        redis=redis if redis is not None else FakeRedis(),
        allow_when_unknown=allow_when_unknown,
    )


def test_canonical_keys():
    assert consumed_key(ORG) == f"bolt-cap:{ORG}:consumed"
    assert limit_key(ORG) == f"bolt-cap:{ORG}:limit"
    assert cycle_id_key(ORG) == f"bolt-cap:{ORG}:cycle_id"
    assert hold_key(ORG, "op-1") == f"bolt-hold:{ORG}:op-1"


def test_usage_rpc_is_required():
    with pytest.raises(ValueError, match="usage_rpc"):
        PlatformBudgetReader(
            supabase_url="https://project.supabase.co",
            service_role_key="key",
            product_slug="smart-qr",
            usage_rpc="",
        )


@pytest.mark.asyncio
async def test_consumed_comes_from_usage_rpc_not_subscription_counter():
    gate = build_gate(Platform(usage_total=42, subscription={"total_bolts_consumed": 9999}))
    budget = await gate.budget(ORG)
    assert budget is not None
    assert budget.consumed_bolts == 42
    assert budget.bolts_per_code == PRICE


@pytest.mark.asyncio
async def test_reserve_holds_priced_amount_and_operation_hold():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    reason, bolts = await gate.reserve(ORG, 3, operation_id="cfg:row:hash")

    assert reason is None
    assert bolts == 3 * PRICE
    assert redis.store[consumed_key(ORG)] == str(USAGE + 3 * PRICE)
    assert redis.store[hold_key(ORG, "cfg:row:hash")] == str(3 * PRICE)


@pytest.mark.asyncio
async def test_release_exact_bolts_does_not_reprice():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    _, held = await gate.reserve(ORG, 2, operation_id="op-a")
    # Simulate a price change in the cached budget by mutating bolts_per_unit
    # indirectly: release with exact bolts must ignore current price.
    await gate.release(ORG, units=99, bolts=held, operation_id="op-a")

    assert redis.store[consumed_key(ORG)] == str(USAGE)
    assert hold_key(ORG, "op-a") not in redis.store


@pytest.mark.asyncio
async def test_free_plan_ceiling_from_allocation():
    gate = build_gate(
        Platform(
            plan_type="individual",
            usage_total=100,
            subscription={"total_bolts_allocated": 100},
        )
    )
    assert await gate.preflight(ORG, 1) == REASON_CEILING


@pytest.mark.asyncio
async def test_unpriced_product_refuses():
    gate = build_gate(Platform(price=None))
    assert await gate.preflight(ORG, 1) == REASON_UNPRICED


@pytest.mark.asyncio
async def test_unreadable_plan_fails_closed():
    gate = build_gate(Platform(fail="get_subscription_status"))
    assert (await gate.reserve(ORG, 1))[0] == REASON_UNKNOWN_BUDGET


@pytest.mark.asyncio
async def test_unreadable_usage_fails_closed():
    gate = build_gate(Platform(fail=DEFAULT_USAGE_RPC))
    assert (await gate.reserve(ORG, 1))[0] == REASON_UNKNOWN_BUDGET


@pytest.mark.asyncio
async def test_transient_platform_error_is_retried():
    platform = Platform(fail="get_subscription_status", fail_status=503)
    gate = build_gate(platform)
    await gate.reserve(ORG, 1)
    assert platform.calls.count("get_subscription_status") == REST_ATTEMPTS


@pytest.mark.asyncio
async def test_redis_outage_fails_closed_by_default():
    gate = build_gate(
        Platform(subscription={"hard_cap_bolt_amount": 500}), ExplodingRedis()
    )
    reason, bolts = await gate.reserve(ORG, 1)
    assert reason == REASON_UNKNOWN_BUDGET
    assert bolts == 0


@pytest.mark.asyncio
async def test_redis_outage_falls_open_only_when_allow_when_unknown():
    gate = build_gate(
        Platform(subscription={"hard_cap_bolt_amount": 500}),
        ExplodingRedis(),
        allow_when_unknown=True,
    )
    reason, bolts = await gate.reserve(ORG, 1)
    assert reason is None
    assert bolts == PRICE


@pytest.mark.asyncio
async def test_plan_suspended_blocks():
    from smarthub_core.billing import REASON_SUSPENDED

    gate = build_gate(Platform(plan_suspended=True))
    assert await gate.preflight(ORG, 1) == REASON_SUSPENDED


@pytest.mark.asyncio
async def test_pending_payment_blocks():
    from smarthub_core.billing import REASON_PENDING_PAYMENT

    gate = build_gate(Platform(bolts_pending_payment=True))
    assert await gate.preflight(ORG, 1) == REASON_PENDING_PAYMENT


@pytest.mark.asyncio
async def test_hard_cap_flag_blocks():
    from smarthub_core.billing import REASON_HARD_CAP

    gate = build_gate(Platform(subscription={"hard_cap_reached": True}))
    assert await gate.preflight(ORG, 1) == REASON_HARD_CAP


@pytest.mark.asyncio
async def test_tools_disabled_blocks():
    from smarthub_core.billing import REASON_TOOLS_DISABLED

    gate = build_gate(Platform(subscription={"can_use_tools": False}))
    assert await gate.preflight(ORG, 1) == REASON_TOOLS_DISABLED


@pytest.mark.asyncio
async def test_paid_plan_without_hard_cap_allows_overage():
    redis = FakeRedis()
    # No hard_cap_bolt_amount, plan_type=team → ceiling None.
    gate = build_gate(Platform(usage_total=9990), redis)
    reason, bolts = await gate.reserve(ORG, 5)
    assert reason is None
    assert bolts == 5 * PRICE
    assert redis.store[consumed_key(ORG)] == str(9990 + 5 * PRICE)


@pytest.mark.asyncio
async def test_release_via_operation_id_hold_lookup():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)
    _, held = await gate.reserve(ORG, 2, operation_id="op-hold")
    assert held == 2 * PRICE
    # Omit bolts= — must read hold metadata.
    await gate.release(ORG, units=99, operation_id="op-hold")
    assert redis.store[consumed_key(ORG)] == str(USAGE)
    assert hold_key(ORG, "op-hold") not in redis.store


@pytest.mark.asyncio
async def test_sequential_reserves_respect_shared_ceiling():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 124}), redis)
    assert (await gate.reserve(ORG, 1))[0] is None  # 100+8=108
    assert (await gate.reserve(ORG, 2))[0] is None  # 108+16=124
    assert (await gate.reserve(ORG, 1))[0] == REASON_INSUFFICIENT
    assert redis.store[consumed_key(ORG)] == "124"


@pytest.mark.asyncio
async def test_no_subscription_blocks():
    from smarthub_core.billing import REASON_NO_SUBSCRIPTION

    gate = build_gate(Platform(no_subscription=True))
    assert await gate.preflight(ORG, 1) == REASON_NO_SUBSCRIPTION


@pytest.mark.asyncio
async def test_inactive_price_is_unpriced():
    gate = build_gate(Platform(price_active=False))
    assert await gate.preflight(ORG, 1) == REASON_UNPRICED


@pytest.mark.asyncio
async def test_cycle_rollover_reseeds_from_usage_baseline():
    redis = FakeRedis()
    redis.store[consumed_key(ORG)] = "900"
    redis.store[cycle_id_key(ORG)] = "2026-06-01T00:00:00+00:00"
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    reason, _ = await gate.reserve(ORG, 1)

    assert reason is None
    assert redis.store[consumed_key(ORG)] == str(USAGE + PRICE)


@pytest.mark.asyncio
async def test_quote_uses_budget():
    billing = BoltBilling(
        build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}))
    )
    quote = await billing.quote(ORG, 10)
    assert quote.bolts_per_code == PRICE
    assert quote.bolts_required == 10 * PRICE
    assert quote.bolts_remaining == 400


@pytest.mark.asyncio
async def test_reserve_helpers_seed_and_release():
    redis = FakeRedis()
    await seed_limit(redis, ORG, 200)
    allowed, headroom = await reserve_bolts(
        redis, ORG, 25, cycle_id=CYCLE, cycle_baseline=50
    )
    assert allowed is True
    assert headroom == 125
    assert redis.store[consumed_key(ORG)] == "75"
    new = await release_bolts(redis, ORG, 25)
    assert new == 50
    assert redis.store[consumed_key(ORG)] == "50"


@pytest.mark.asyncio
async def test_hard_cap_denies_without_mutating():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 120}), redis)
    assert (await gate.reserve(ORG, 2))[0] is None
    assert (await gate.reserve(ORG, 1))[0] == REASON_INSUFFICIENT
    # First reserve of 2*8=16 → 100+16=116; second denied leaves counter alone.
    assert redis.store[consumed_key(ORG)] == str(USAGE + 2 * PRICE)
