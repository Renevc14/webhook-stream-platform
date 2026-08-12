"""Reads plan, caps, price and current-cycle usage from the Hub (service role)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

import httpx

from smarthub_core.billing.models import OrgBudget
from smarthub_core.billing.reasons import (
    REASON_HARD_CAP,
    REASON_NO_SUBSCRIPTION,
    REASON_PENDING_PAYMENT,
    REASON_SUSPENDED,
    REASON_TOOLS_DISABLED,
    REASON_UNPRICED,
)

log = logging.getLogger(__name__)

REST_TIMEOUT_S = 8.0
REST_ATTEMPTS = 3

# Sentinel for "the product has no usable price", as opposed to a failed read.
UNPRICED = -1

# Canonical RPC that sums current-cycle bolts across billing tools.
DEFAULT_USAGE_RPC = "get_org_billing_usage_total"


class PlatformBudgetReader:
    """Reads plan, caps, price and actual usage from the Hub with the service role.

    ``usage_rpc`` is required: the Redis cycle baseline must come from real
    billing usage (usage logs aggregated by the Hub), not from
    ``get_subscription_status.total_bolts_consumed``, which can lag or stall.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        product_slug: str,
        usage_rpc: str,
        timeout_s: float = REST_TIMEOUT_S,
    ) -> None:
        if not (usage_rpc or "").strip():
            raise ValueError(
                "usage_rpc is required (e.g. get_org_billing_usage_total)"
            )
        self._base_url = supabase_url.rstrip("/")
        self._key = service_role_key
        self._product_slug = product_slug
        self._usage_rpc = usage_rpc.strip()
        self._timeout_s = timeout_s
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self._base_url}/rest/v1",
                timeout=self._timeout_s,
                headers={
                    "apikey": self._key,
                    "Authorization": f"Bearer {self._key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def read(self, organization_id: UUID) -> Optional[OrgBudget]:
        """The org's budget, or None when the platform could not be read.

        None is not "no budget": callers decide what to do when the answer is
        unknown.
        """
        status_rows = await self._request(
            "POST",
            "/rpc/get_subscription_status",
            json={"p_organization_id": str(organization_id)},
        )
        if status_rows is None:
            return None

        price = await self._bolts_per_unit()
        if price is None:
            return None
        if price == UNPRICED:
            return OrgBudget(
                bolts_per_unit=0,
                allocated_bolts=0,
                consumed_bolts=0,
                ceiling_bolts=0,
                cycle_id="",
                blocked_reason=REASON_UNPRICED,
            )

        cycle_id = ""
        if not status_rows:
            return OrgBudget(
                bolts_per_unit=price,
                allocated_bolts=0,
                consumed_bolts=0,
                ceiling_bolts=0,
                cycle_id=cycle_id,
                blocked_reason=REASON_NO_SUBSCRIPTION,
            )

        status = status_rows[0]
        cycle_id = str(status.get("current_cycle_start") or "")
        allocated = _as_int(
            status.get("current_cycle_total_bolts_allocated")
            or status.get("total_bolts_allocated")
        )

        consumed = await self._current_cycle_usage(organization_id)
        if consumed is None:
            return None

        flags = await self._request(
            "GET",
            "/organization_subscriptions",
            params={
                "select": "plan_suspended,bolts_pending_payment",
                "organization_id": f"eq.{organization_id}",
                "limit": "1",
            },
        )
        if flags is None:
            return None
        suspended = bool(flags and flags[0].get("plan_suspended"))
        pending_payment = bool(flags and flags[0].get("bolts_pending_payment"))

        orgs = await self._request(
            "GET",
            "/organizations",
            params={
                "select": "plan_type",
                "id": f"eq.{organization_id}",
                "limit": "1",
            },
        )
        if orgs is None:
            return None
        is_free_plan = bool(orgs and str(orgs[0].get("plan_type") or "") == "individual")

        blocked: Optional[str] = None
        if suspended:
            blocked = REASON_SUSPENDED
        elif pending_payment:
            blocked = REASON_PENDING_PAYMENT
        elif bool(status.get("hard_cap_reached")):
            blocked = REASON_HARD_CAP
        elif status.get("can_use_tools") is False:
            blocked = REASON_TOOLS_DISABLED

        hard_cap_amount = _as_int(status.get("hard_cap_bolt_amount"))
        if hard_cap_amount > 0:
            ceiling: Optional[int] = hard_cap_amount
        elif is_free_plan:
            ceiling = allocated
        else:
            ceiling = None

        budget = OrgBudget(
            bolts_per_unit=price,
            allocated_bolts=allocated,
            consumed_bolts=consumed,
            ceiling_bolts=ceiling,
            cycle_id=cycle_id,
            blocked_reason=blocked,
        )
        log.info(
            "Budget org=%s product=%s price=%s allocated=%s consumed=%s "
            "ceiling=%s free=%s blocked=%s usage_rpc=%s",
            organization_id,
            self._product_slug,
            price,
            allocated,
            consumed,
            "none" if ceiling is None else ceiling,
            is_free_plan,
            blocked or "-",
            self._usage_rpc,
        )
        return budget

    async def _current_cycle_usage(self, organization_id: UUID) -> Optional[int]:
        """Actual bolts consumed this cycle from the shared usage RPC."""
        rows = await self._request(
            "POST",
            f"/rpc/{self._usage_rpc}",
            json={"p_organization_id": str(organization_id)},
        )
        if rows is None:
            return None
        return _parse_usage_total(rows)

    async def _bolts_per_unit(self) -> Optional[int]:
        """Price of one primary event, from the platform's pricing list.

        Never defaulted: charging a made-up number is worse than refusing to
        charge. Returns UNPRICED when there is no usable row and None when the
        read itself failed.
        """
        rows = await self._request(
            "GET",
            "/pricing_list",
            params={
                "select": "primary_event_bolts,is_active",
                "product_slug": f"eq.{self._product_slug}",
                "limit": "1",
            },
        )
        if rows is None:
            return None
        if not rows or rows[0].get("is_active") is False:
            log.error(
                "No active pricing_list row for product_slug=%s — cannot meter bolts",
                self._product_slug,
            )
            return UNPRICED
        price = _as_int(rows[0].get("primary_event_bolts"))
        if price <= 0:
            log.error(
                "pricing_list.primary_event_bolts for %s is %s — refusing to meter",
                self._product_slug,
                price,
            )
            return UNPRICED
        return price

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Optional[list[dict[str, Any]]]:
        last_error: Optional[str] = None
        for attempt in range(REST_ATTEMPTS):
            try:
                resp = await self._get_client().request(
                    method, path, params=params, json=json
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, list):
                        return payload
                    if isinstance(payload, dict):
                        return [payload]
                    if isinstance(payload, (int, float)):
                        return [{"total_bolts": int(payload)}]
                    return []
                last_error = f"HTTP {resp.status_code} {resp.text[:200]}"
                if resp.status_code < 500:
                    log.error(
                        "Platform billing read of %s rejected: %s", path, last_error
                    )
                    return None
            if attempt < REST_ATTEMPTS - 1:
                delay = 0.2 * (2**attempt)
                log.warning(
                    "Platform billing read of %s failed (attempt %s, retry in %.1fs): %s",
                    path,
                    attempt + 1,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)
        log.error("Platform billing read of %s failed: %s", path, last_error)
        return None


def _parse_usage_total(rows: list[Any]) -> int:
    """Extract a bolt total from the usage RPC response."""
    if not rows:
        return 0
    first = rows[0]
    if isinstance(first, (int, float)):
        return max(0, int(first))
    if not isinstance(first, dict):
        return 0
    for key in (
        "total_bolts",
        "total",
        "_total",
        "bolts_consumed",
        "consumed_bolts",
        "get_org_billing_usage_total",
    ):
        if key in first and first[key] is not None:
            return max(0, _as_int(first[key]))
    # Scalar RPC sometimes returns under the function name as the only key.
    if len(first) == 1:
        return max(0, _as_int(next(iter(first.values()))))
    return 0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
