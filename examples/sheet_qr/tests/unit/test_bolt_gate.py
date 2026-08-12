"""Bolt metering: what blocks a run, what it costs, and what gets refunded.

The platform reads go through a mocked PostgREST, and Redis is replaced by a
stand-in that mirrors ``RedisClient.atomic_*`` (allow/deny plus cycle reset).
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import httpx
import pytest

from smart_qr.adapters.billing import bolt_gate
from smart_qr.adapters.billing.bolt_gate import (
    DEFAULT_USAGE_RPC,
    REASON_CEILING,
    REASON_HARD_CAP,
    REASON_INSUFFICIENT,
    REASON_NO_SUBSCRIPTION,
    REASON_PENDING_PAYMENT,
    REASON_SUSPENDED,
    REASON_TOOLS_DISABLED,
    REASON_UNKNOWN_BUDGET,
    REASON_UNPRICED,
    BoltGate,
    PlatformBudgetReader,
)
from smart_qr.adapters.billing.port import BoltBilling, MockBilling, build_billing
from smarthub_core.billing import consumed_key, cycle_id_key, hold_key
from smarthub_core.db.redis import RedisHelperError

ORG = UUID("11111111-1111-1111-1111-111111111111")
CYCLE = "2026-07-01T00:00:00+00:00"
PRICE = 8
USAGE = 100


class Platform:
    """PostgREST stand-in for the billing reads."""

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
    ):
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
                "total_bolts_consumed": 9999,
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
                json=[{"primary_event_bolts": self.price, "is_active": self.price_active}],
            )
        raise AssertionError(f"unexpected billing read: {path}")


class FakeRaw:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = str(value)
        return True

    async def delete(self, *keys):
        n = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                n += 1
        return n


class FakeRedis:
    """Mirrors RedisClient.atomic_* without a server."""

    def __init__(self):
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

    async def atomic_deduct(self, consumed, limit, requested):
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
        self, consumed, limit, cycle, requested, expected_cycle, baseline
    ):
        self.evals += 1
        if self.store.get(cycle) != expected_cycle:
            self.store[consumed] = str(baseline)
            self.store[cycle] = expected_cycle
        return await self.atomic_deduct(consumed, limit, requested)

    async def atomic_release(self, consumed, amount):
        self.evals += 1
        cur = int(self.store.get(consumed, "0"))
        new = max(0, cur - amount)
        self.store[consumed] = str(new)
        return new


class ExplodingRedis(FakeRedis):
    async def atomic_deduct_with_cycle(self, *a, **k):
        raise RedisHelperError("redis is down")

    async def atomic_deduct(self, *a, **k):
        raise RedisHelperError("redis is down")

    async def atomic_release(self, *a, **k):
        raise RedisHelperError("redis is down")


def build_gate(platform: Platform, redis=None, *, allow_when_unknown=False) -> BoltGate:
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


@pytest.mark.asyncio
async def test_price_comes_from_the_platform_not_from_code():
    gate = build_gate(Platform())
    budget = await gate.budget(ORG)
    assert budget.bolts_per_code == PRICE


@pytest.mark.asyncio
async def test_consumed_baseline_is_usage_rpc_not_subscription_counter():
    gate = build_gate(Platform(usage_total=77, subscription={"total_bolts_consumed": 1}))
    budget = await gate.budget(ORG)
    assert budget.consumed_bolts == 77


@pytest.mark.asyncio
async def test_reserve_holds_the_priced_amount():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    reason, bolts = await gate.reserve(ORG, 3)

    assert reason is None
    assert bolts == 3 * PRICE
    assert redis.store[consumed_key(ORG)] == str(USAGE + 3 * PRICE)


@pytest.mark.asyncio
async def test_release_gives_the_exact_bolts_back():
    redis = FakeRedis()
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    _, held = await gate.reserve(ORG, 2, operation_id="op-1")
    await gate.release(ORG, bolts=held, operation_id="op-1")

    assert redis.store[consumed_key(ORG)] == str(USAGE)
    assert hold_key(ORG, "op-1") not in redis.store


@pytest.mark.asyncio
async def test_reserve_stops_at_the_hard_cap_amount():
    redis = FakeRedis()
    gate = build_gate(
        Platform(subscription={"hard_cap_bolt_amount": 120}), redis
    )

    assert (await gate.reserve(ORG, 2))[0] is None
    assert (await gate.reserve(ORG, 1))[0] == REASON_INSUFFICIENT


@pytest.mark.asyncio
async def test_a_free_plan_cannot_go_into_overage():
    gate = build_gate(
        Platform(
            plan_type="individual",
            usage_total=100,
            subscription={"total_bolts_allocated": 100},
        )
    )
    assert await gate.preflight(ORG, 1) == REASON_CEILING


@pytest.mark.asyncio
async def test_a_paid_plan_without_a_hard_cap_may_overage():
    gate = build_gate(
        Platform(
            plan_type="team",
            usage_total=5000,
            subscription={"total_bolts_allocated": 100},
        )
    )
    budget = await gate.budget(ORG)
    assert budget.ceiling_bolts is None
    assert budget.remaining_bolts is None
    assert await gate.preflight(ORG, 50) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,expected",
    [
        (Platform(plan_suspended=True), REASON_SUSPENDED),
        (Platform(bolts_pending_payment=True), REASON_PENDING_PAYMENT),
        (Platform(subscription={"hard_cap_reached": True}), REASON_HARD_CAP),
        (Platform(subscription={"can_use_tools": False}), REASON_TOOLS_DISABLED),
        (Platform(no_subscription=True), REASON_NO_SUBSCRIPTION),
    ],
)
async def test_plan_state_blocks_work_with_a_stable_reason(platform, expected):
    gate = build_gate(platform)
    assert await gate.preflight(ORG, 1) == expected
    assert (await gate.reserve(ORG, 1))[0] == expected


@pytest.mark.asyncio
async def test_denied_reservation_holds_nothing():
    redis = FakeRedis()
    gate = build_gate(Platform(plan_suspended=True), redis)

    await gate.reserve(ORG, 5)

    assert redis.evals == 0
    assert redis.store == {}


@pytest.mark.asyncio
async def test_an_unpriced_product_refuses_rather_than_guessing():
    gate = build_gate(Platform(price=None))
    assert await gate.preflight(ORG, 1) == REASON_UNPRICED

    gate = build_gate(Platform(price_active=False))
    assert await gate.preflight(ORG, 1) == REASON_UNPRICED

    gate = build_gate(Platform(price=0))
    assert await gate.preflight(ORG, 1) == REASON_UNPRICED


@pytest.mark.asyncio
async def test_unreadable_plan_fails_closed_outside_local():
    gate = build_gate(Platform(fail="get_subscription_status"))
    assert (await gate.reserve(ORG, 1))[0] == REASON_UNKNOWN_BUDGET


@pytest.mark.asyncio
async def test_unreadable_plan_is_allowed_in_local():
    gate = build_gate(Platform(fail="get_subscription_status"), allow_when_unknown=True)
    reason, bolts = await gate.reserve(ORG, 1)
    assert reason is None
    assert bolts == 0


@pytest.mark.asyncio
async def test_a_rejected_service_key_is_not_retried():
    platform = Platform(fail="get_subscription_status", fail_status=401)
    gate = build_gate(platform)
    await gate.reserve(ORG, 1)
    assert platform.calls.count("get_subscription_status") == 1


@pytest.mark.asyncio
async def test_transient_platform_error_is_retried():
    platform = Platform(fail="get_subscription_status", fail_status=503)
    gate = build_gate(platform)
    await gate.reserve(ORG, 1)
    assert platform.calls.count("get_subscription_status") == bolt_gate.REST_ATTEMPTS


@pytest.mark.asyncio
async def test_a_redis_outage_fails_closed_outside_local():
    """Deployed tools must not overspend a shared Postgres snapshot when Redis is down."""
    gate = build_gate(
        Platform(subscription={"hard_cap_bolt_amount": 500}), ExplodingRedis()
    )
    reason, bolts = await gate.reserve(ORG, 1)
    assert reason == bolt_gate.REASON_UNKNOWN_BUDGET
    assert bolts == 0


@pytest.mark.asyncio
async def test_a_redis_outage_may_fall_open_in_local_only():
    gate = build_gate(
        Platform(subscription={"hard_cap_bolt_amount": 500}),
        ExplodingRedis(),
        allow_when_unknown=True,
    )
    reason, bolts = await gate.reserve(ORG, 1)
    assert reason is None
    assert bolts == PRICE


@pytest.mark.asyncio
async def test_the_plan_is_read_once_per_cache_window():
    platform = Platform()
    gate = build_gate(platform)

    await gate.preflight(ORG, 1)
    await gate.preflight(ORG, 1)
    assert platform.calls.count("get_subscription_status") == 1
    assert platform.calls.count(DEFAULT_USAGE_RPC) == 1

    gate.invalidate(ORG)
    await gate.preflight(ORG, 1)
    assert platform.calls.count("get_subscription_status") == 2


@pytest.mark.asyncio
async def test_a_new_cycle_resets_the_redis_counter_from_usage_baseline():
    redis = FakeRedis()
    redis.store[consumed_key(ORG)] = "900"
    redis.store[cycle_id_key(ORG)] = "2026-06-01T00:00:00+00:00"
    gate = build_gate(Platform(subscription={"hard_cap_bolt_amount": 500}), redis)

    reason, _ = await gate.reserve(ORG, 1)

    assert reason is None
    assert redis.store[consumed_key(ORG)] == str(USAGE + PRICE)


@pytest.mark.asyncio
async def test_quote_prices_a_run_before_it_starts():
    billing = BoltBilling(build_gate(Platform(subscription={"hard_cap_bolt_amount": 500})))
    quote = await billing.quote(ORG, 10)
    assert quote.bolts_per_code == PRICE
    assert quote.bolts_required == 10 * PRICE
    assert quote.bolts_remaining == 400
    assert quote.blocked_reason is None


@pytest.mark.asyncio
async def test_quote_reports_why_a_run_would_be_refused():
    billing = BoltBilling(
        build_gate(Platform(subscription={"hard_cap_bolt_amount": 120}))
    )
    quote = await billing.quote(ORG, 100)
    assert quote.blocked_reason == REASON_INSUFFICIENT


@pytest.mark.asyncio
async def test_an_unpriced_product_is_not_billed_even_in_local():
    gate = build_gate(Platform(price=None), allow_when_unknown=True)
    assert (await gate.reserve(ORG, 1))[0] == REASON_UNPRICED


@pytest.mark.asyncio
async def test_mock_billing_fails_fast_outside_local():
    class Settings:
        smartqr_billing_mode = "mock"
        is_local = False
        app_env = "staging"

    with pytest.raises(RuntimeError, match="SMARTQR_BILLING_MODE=mock"):
        await build_billing(Settings())


@pytest.mark.asyncio
async def test_mock_billing_allowed_in_local():
    class Settings:
        smartqr_billing_mode = "mock"
        is_local = True
        app_env = "local"

    billing = await build_billing(Settings())
    assert isinstance(billing, MockBilling)
