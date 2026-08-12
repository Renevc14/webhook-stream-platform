"""Stable denial strings for bolt metering.

These reach the Hub as ``error_code`` values and are matched in logs — do not
reword them casually.
"""

from __future__ import annotations

REASON_SUSPENDED = "plan_suspended"
REASON_PENDING_PAYMENT = "bolts_pending_payment"
REASON_HARD_CAP = "hard_cap_reached"
REASON_TOOLS_DISABLED = "plan_cannot_use_tools"
REASON_CEILING = "bolt_ceiling_reached"
REASON_INSUFFICIENT = "insufficient_bolts"
REASON_UNPRICED = "product_not_priced"
REASON_NO_SUBSCRIPTION = "no_subscription"
REASON_UNKNOWN_BUDGET = "budget_unavailable"
