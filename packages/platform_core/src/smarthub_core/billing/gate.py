"""Bolt gate: decide whether an org may spend, and hold bolts while it works."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from uuid import UUID

from smarthub_core.billing.models import OrgBudget
from smarthub_core.billing.reader import PlatformBudgetReader
from smarthub_core.billing.reasons import REASON_INSUFFICIENT, REASON_UNKNOWN_BUDGET
from smarthub_core.billing.reserve import (
    clear_hold,
    read_hold,
    record_hold,
    release_bolts,
    reserve_bolts,
    seed_limit,
)
from smarthub_core.db.redis import RedisHelperError

log = logging.getLogger(__name__)

CONTEXT_CACHE_TTL_S = 30.0


class BoltGate:
    """Decides whether an org may spend units, and holds the bolts while it does."""

    def __init__(
        self,
        *,
        reader: PlatformBudgetReader,
        redis: Any,
        cache_ttl_s: float = CONTEXT_CACHE_TTL_S,
        allow_when_unknown: bool = False,
    ) -> None:
        self._reader = reader
        self._redis = redis
        self._cache_ttl_s = cache_ttl_s
        # Only local development may bill optimistically when the platform is
        # unreachable; deployed environments must not give away bolts.
        self._allow_when_unknown = allow_when_unknown
        self._cache: dict[str, tuple[float, OrgBudget]] = {}

    async def aclose(self) -> None:
        await self._reader.aclose()

    def invalidate(self, organization_id: UUID) -> None:
        self._cache.pop(str(organization_id), None)

    async def budget(self, organization_id: UUID) -> Optional[OrgBudget]:
        key = str(organization_id)
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._cache_ttl_s:
            return hit[1]
        budget = await self._reader.read(organization_id)
        if budget is not None:
            self._cache[key] = (time.monotonic(), budget)
        return budget

    async def preflight(self, organization_id: UUID, units: int = 1) -> Optional[str]:
        """Why this work cannot start, or None when it can. Reserves nothing."""
        budget = await self.budget(organization_id)
        if budget is None:
            return None if self._allow_when_unknown else REASON_UNKNOWN_BUDGET
        return budget.denial_for(units)

    async def reserve(
        self,
        organization_id: UUID,
        units: int = 1,
        *,
        operation_id: Optional[str] = None,
    ) -> tuple[Optional[str], int]:
        """Hold the bolts for ``units``.

        Returns ``(denial reason or None, bolts held)``. When ``operation_id`` is
        set, the exact amount is also stored under a ``bolt-hold`` key (TTL is
        metadata only — it does not auto-refund the aggregate consumed counter).
        """
        budget = await self.budget(organization_id)
        if budget is None:
            if self._allow_when_unknown:
                log.warning(
                    "Budget unknown for org=%s — allowing in local only",
                    organization_id,
                )
                return None, 0
            log.error(
                "Budget unknown for org=%s — refusing to spend", organization_id
            )
            return REASON_UNKNOWN_BUDGET, 0

        denial = budget.denial_for(units)
        if denial:
            return denial, 0

        amount = units * budget.bolts_per_unit
        try:
            await seed_limit(self._redis, organization_id, budget.ceiling_bolts)
            allowed, headroom = await reserve_bolts(
                self._redis,
                organization_id,
                amount,
                cycle_id=budget.cycle_id,
                cycle_baseline=budget.consumed_bolts,
            )
        except (RedisHelperError, Exception) as exc:  # noqa: BLE001
            # Redis is the shared concurrency gate across tools. Falling open
            # when it is down lets every replica/tool overspend the same
            # Postgres snapshot. Only local (allow_when_unknown) may proceed.
            log.error(
                "Redis reservation failed org=%s amount=%s (%s)",
                organization_id,
                amount,
                exc,
            )
            if self._allow_when_unknown:
                log.warning(
                    "Redis reservation failed org=%s — allowing in local only "
                    "(Postgres snapshot; no cross-task precision)",
                    organization_id,
                )
                return None, amount
            return REASON_UNKNOWN_BUDGET, 0

        if not allowed:
            log.warning(
                "Bolt reservation denied org=%s wanted=%s headroom=%s",
                organization_id,
                amount,
                headroom,
            )
            return REASON_INSUFFICIENT, 0

        if operation_id:
            try:
                await record_hold(self._redis, organization_id, operation_id, amount)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not record bolt hold org=%s operation_id=%s: %s "
                    "(release must still use the Reservation.bolts amount)",
                    organization_id,
                    operation_id,
                    exc,
                )

        log.info(
            "Reserved %s bolts org=%s units=%s headroom=%s operation_id=%s",
            amount,
            organization_id,
            units,
            headroom,
            operation_id or "-",
        )
        return None, amount

    async def release(
        self,
        organization_id: UUID,
        units: int = 1,
        *,
        bolts: Optional[int] = None,
        operation_id: Optional[str] = None,
    ) -> None:
        """Give back bolts held for work that failed (never charge for nothing).

        Prefer ``bolts=`` (exact held amount from the Reservation) or
        ``operation_id=`` (lookup of the hold key). Falling back to
        ``units * current price`` can mis-refund if pricing changed mid-flight.
        """
        amount = bolts
        if amount is None and operation_id:
            try:
                amount = await read_hold(self._redis, organization_id, operation_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not read bolt hold org=%s operation_id=%s: %s",
                    organization_id,
                    operation_id,
                    exc,
                )
                amount = None

        if amount is None:
            budget = await self.budget(organization_id)
            if budget is None:
                log.warning(
                    "Cannot price a refund for org=%s (budget unknown) — Redis will "
                    "reconcile at the next cycle",
                    organization_id,
                )
                return
            amount = units * budget.bolts_per_unit

        if amount <= 0:
            return

        try:
            await release_bolts(self._redis, organization_id, amount)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Bolt refund failed org=%s amount=%s: %s",
                organization_id,
                amount,
                exc,
            )
            return

        if operation_id:
            try:
                await clear_hold(self._redis, organization_id, operation_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not clear bolt hold org=%s operation_id=%s: %s",
                    organization_id,
                    operation_id,
                    exc,
                )

        log.info(
            "Refunded %s bolts org=%s units=%s operation_id=%s",
            amount,
            organization_id,
            units,
            operation_id or "-",
        )
