"""Bolt metering for SmartQR — thin re-export of ``smarthub_core.billing``.

The implementation (plan reads, usage baseline via ``get_org_billing_usage_total``,
Redis ``bolt-cap:{org}:*`` via ``RedisClient.atomic_*``) lives in core so every
tool shares one contract. This module keeps the historical import path stable
for SmartQR tests and application code.
"""

from __future__ import annotations

from smarthub_core.billing import (
    CONSUMED_KEY,
    CONTEXT_CACHE_TTL_S,
    CYCLE_KEY,
    DEFAULT_USAGE_RPC,
    LIMIT_KEY,
    LIMIT_TTL_S,
    REASON_CEILING,
    REASON_HARD_CAP,
    REASON_INSUFFICIENT,
    REASON_NO_SUBSCRIPTION,
    REASON_PENDING_PAYMENT,
    REASON_SUSPENDED,
    REASON_TOOLS_DISABLED,
    REASON_UNKNOWN_BUDGET,
    REASON_UNPRICED,
    REST_ATTEMPTS,
    REST_TIMEOUT_S,
    UNPRICED,
    BoltGate,
    OrgBudget,
    PlatformBudgetReader,
)

__all__ = [
    "CONSUMED_KEY",
    "CONTEXT_CACHE_TTL_S",
    "CYCLE_KEY",
    "DEFAULT_USAGE_RPC",
    "LIMIT_KEY",
    "LIMIT_TTL_S",
    "REASON_CEILING",
    "REASON_HARD_CAP",
    "REASON_INSUFFICIENT",
    "REASON_NO_SUBSCRIPTION",
    "REASON_PENDING_PAYMENT",
    "REASON_SUSPENDED",
    "REASON_TOOLS_DISABLED",
    "REASON_UNKNOWN_BUDGET",
    "REASON_UNPRICED",
    "REST_ATTEMPTS",
    "REST_TIMEOUT_S",
    "UNPRICED",
    "BoltGate",
    "OrgBudget",
    "PlatformBudgetReader",
]
