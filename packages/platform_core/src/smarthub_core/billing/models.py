"""Shared billing value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from smarthub_core.billing.reasons import REASON_CEILING, REASON_INSUFFICIENT


@dataclass(frozen=True)
class OrgBudget:
    """What one organization may still spend this cycle."""

    bolts_per_unit: int
    allocated_bolts: int
    consumed_bolts: int
    # None = no ceiling: a paid plan without a hard cap may go into overage.
    ceiling_bolts: Optional[int]
    cycle_id: str
    blocked_reason: Optional[str]

    @property
    def bolts_per_code(self) -> int:
        """Alias kept for SmartQR / wizard callers that price "codes"."""
        return self.bolts_per_unit

    @property
    def remaining_bolts(self) -> Optional[int]:
        if self.ceiling_bolts is None:
            return None
        return max(0, self.ceiling_bolts - self.consumed_bolts)

    def denial_for(self, units: int) -> Optional[str]:
        """Why this many units cannot be generated, or None when they can."""
        if self.blocked_reason:
            return self.blocked_reason
        remaining = self.remaining_bolts
        if remaining is None:
            return None
        if remaining <= 0:
            return REASON_CEILING
        if remaining < units * self.bolts_per_unit:
            return REASON_INSUFFICIENT
        return None


@dataclass(frozen=True)
class Reservation:
    """Whether the bolts were held, and if not, why — the reason reaches the user.

    ``bolts`` is what was actually held, so the usage row records the real price
    rather than assuming one bolt per unit. Prefer releasing with this exact
    amount (or via ``operation_id``) so a price change mid-flight cannot reprice
    the refund.
    """

    allowed: bool
    reason: Optional[str] = None
    bolts: int = 0
    operation_id: Optional[str] = None


@dataclass(frozen=True)
class Quote:
    """What a run would cost, so the wizard can say it before anyone clicks."""

    bolts_per_code: int
    bolts_required: int
    # None = unknown (mocked) or unlimited (paid plan without a hard cap).
    bolts_remaining: Optional[int] = None
    blocked_reason: Optional[str] = None
