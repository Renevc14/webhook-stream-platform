"""Opt-in org-wide bolt metering for Smarthub tools.

Postgres (usage logs + Hub RPCs) is the source of truth for invoices.
Redis ``bolt-cap:{org}:*`` is the shared concurrency gate. Import this package
explicitly — tools that have not migrated keep their local bolt_cap and are
unaffected.

Hold keys (``bolt-hold:{org}:{operation_id}``) store the exact bolts reserved
for one operation so release does not reprice. Their TTL expires metadata only;
it does **not** auto-decrement the aggregate consumed counter.

When Redis is unreachable, ``BoltGate.reserve`` fails closed unless
``allow_when_unknown=True`` (local only). Falling open would let every
replica/tool overspend the same Postgres snapshot.
"""

from __future__ import annotations

from smarthub_core.billing.gate import CONTEXT_CACHE_TTL_S, BoltGate
from smarthub_core.billing.keys import (
    CONSUMED_KEY,
    CYCLE_KEY,
    HOLD_KEY,
    HOLD_TTL_S,
    LIMIT_KEY,
    LIMIT_TTL_S,
    consumed_key,
    cycle_id_key,
    hold_key,
    limit_key,
)
from smarthub_core.billing.models import OrgBudget, Quote, Reservation
from smarthub_core.billing.port import BillingPort, BoltBilling, MockBilling
from smarthub_core.billing.reader import (
    DEFAULT_USAGE_RPC,
    REST_ATTEMPTS,
    REST_TIMEOUT_S,
    UNPRICED,
    PlatformBudgetReader,
)
from smarthub_core.billing.reasons import (
    REASON_CEILING,
    REASON_HARD_CAP,
    REASON_INSUFFICIENT,
    REASON_NO_SUBSCRIPTION,
    REASON_PENDING_PAYMENT,
    REASON_SUSPENDED,
    REASON_TOOLS_DISABLED,
    REASON_UNKNOWN_BUDGET,
    REASON_UNPRICED,
)
from smarthub_core.billing.reserve import (
    clear_hold,
    read_hold,
    record_hold,
    release_bolts,
    reserve_bolts,
    seed_limit,
)

__all__ = [
    "BillingPort",
    "BoltBilling",
    "BoltGate",
    "CONTEXT_CACHE_TTL_S",
    "CONSUMED_KEY",
    "CYCLE_KEY",
    "DEFAULT_USAGE_RPC",
    "HOLD_KEY",
    "HOLD_TTL_S",
    "LIMIT_KEY",
    "LIMIT_TTL_S",
    "MockBilling",
    "OrgBudget",
    "PlatformBudgetReader",
    "Quote",
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
    "Reservation",
    "UNPRICED",
    "clear_hold",
    "consumed_key",
    "cycle_id_key",
    "hold_key",
    "limit_key",
    "read_hold",
    "record_hold",
    "release_bolts",
    "reserve_bolts",
    "seed_limit",
]
