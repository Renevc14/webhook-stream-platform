"""Billing port: reserve before work, release on failure, quote for the wizard.

There is no separate "commit": the durable charge is the tool's usage_log row,
which is what the Hub aggregates to invoice. Redis only gates concurrency.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from uuid import UUID

from smarthub_core.billing.models import Quote, Reservation
from smarthub_core.billing.reasons import REASON_UNKNOWN_BUDGET

log = logging.getLogger(__name__)


class BillingPort(Protocol):
    async def reserve(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        operation_id: Optional[str] = None,
    ) -> Reservation: ...

    async def release(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        bolts: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> None: ...

    async def preflight(
        self, organization_id: UUID, amount: int = 1
    ) -> Optional[str]: ...

    async def quote(self, organization_id: UUID, codes: int) -> Quote: ...


class MockBilling:
    """Allows everything. Local development and unit tests only."""

    def __init__(self) -> None:
        self.reserved: dict[str, int] = {}

    async def aclose(self) -> None:
        return None

    async def reserve(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        operation_id: Optional[str] = None,
    ) -> Reservation:
        key = str(organization_id)
        self.reserved[key] = self.reserved.get(key, 0) + amount
        log.info(
            "MockBilling reserve org=%s amount=%s operation_id=%s",
            organization_id,
            amount,
            operation_id or "-",
        )
        return Reservation(
            allowed=True, bolts=amount, operation_id=operation_id
        )

    async def release(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        bolts: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> None:
        refund = bolts if bolts is not None else amount
        key = str(organization_id)
        self.reserved[key] = max(0, self.reserved.get(key, 0) - refund)
        log.info(
            "MockBilling release org=%s amount=%s bolts=%s operation_id=%s",
            organization_id,
            amount,
            refund,
            operation_id or "-",
        )

    async def preflight(
        self, organization_id: UUID, amount: int = 1
    ) -> Optional[str]:
        return None

    async def quote(self, organization_id: UUID, codes: int) -> Quote:
        return Quote(bolts_per_code=1, bolts_required=codes)


class BoltBilling:
    """Real metering against the organization's plan (see ``BoltGate``)."""

    def __init__(self, gate: Any) -> None:
        self._gate = gate

    async def aclose(self) -> None:
        close = getattr(self._gate, "aclose", None)
        if close is not None:
            await close()

    async def reserve(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        operation_id: Optional[str] = None,
    ) -> Reservation:
        reason, bolts = await self._gate.reserve(
            organization_id, amount, operation_id=operation_id
        )
        return Reservation(
            allowed=reason is None,
            reason=reason,
            bolts=bolts,
            operation_id=operation_id,
        )

    async def release(
        self,
        organization_id: UUID,
        amount: int = 1,
        *,
        bolts: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> None:
        await self._gate.release(
            organization_id,
            amount,
            bolts=bolts,
            operation_id=operation_id,
        )

    async def preflight(
        self, organization_id: UUID, amount: int = 1
    ) -> Optional[str]:
        return await self._gate.preflight(organization_id, amount)

    async def quote(self, organization_id: UUID, codes: int) -> Quote:
        budget = await self._gate.budget(organization_id)
        if budget is None:
            return Quote(
                bolts_per_code=0,
                bolts_required=0,
                blocked_reason=REASON_UNKNOWN_BUDGET,
            )
        return Quote(
            bolts_per_code=budget.bolts_per_unit,
            bolts_required=codes * budget.bolts_per_unit,
            bolts_remaining=budget.remaining_bolts,
            blocked_reason=budget.denial_for(codes),
        )
