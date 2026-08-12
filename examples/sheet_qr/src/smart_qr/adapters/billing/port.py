"""Billing port: reserve before work, release on failure, quote for the wizard.

Thin SmartQR adapter over ``smarthub_core.billing``. Construction reuses the
shared core Redis client so bolt-cap keys share one connection pool with the
Smartsheet rate limiter.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from smarthub_core.billing import (
    DEFAULT_USAGE_RPC,
    BillingPort,
    BoltBilling,
    MockBilling,
    Quote,
    Reservation,
)

log = logging.getLogger(__name__)

# Re-export for existing SmartQR imports / tests.
__all__ = [
    "BillingPort",
    "BoltBilling",
    "MockBilling",
    "Quote",
    "Reservation",
    "build_billing",
    "get_billing",
    "reset_billing",
]

_billing: Optional[BillingPort] = None
_billing_built = False


async def build_billing(settings: Any) -> BillingPort:
    """Real metering unless explicitly mocked; never silently free in staging."""
    mode = (settings.smartqr_billing_mode or "").strip().lower()
    if mode == "mock":
        if not settings.is_local:
            raise RuntimeError(
                f"SMARTQR_BILLING_MODE=mock is not allowed in env={settings.app_env} "
                "— set SMARTQR_BILLING_MODE=bolts or use APP_ENV=local"
            )
        log.info("Billing is mocked (local)")
        return MockBilling()

    if not (settings.supabase_url and settings.supabase_service_role_key):
        log.error(
            "SMARTQR_BILLING_MODE=%s but SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are "
            "missing — the plan cannot be read, so every request will be refused",
            mode or "bolts",
        )

    from smarthub_core.billing import BoltGate, PlatformBudgetReader

    from smart_qr.adapters.core_infra import get_core_redis

    reader = PlatformBudgetReader(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        product_slug=settings.product_slug,
        usage_rpc=DEFAULT_USAGE_RPC,
    )
    redis = await get_core_redis(settings)
    gate = BoltGate(
        reader=reader,
        redis=redis,
        allow_when_unknown=settings.is_local,
    )
    log.info(
        "Bolt metering enabled for product_slug=%s usage_rpc=%s",
        settings.product_slug,
        DEFAULT_USAGE_RPC,
    )
    return BoltBilling(gate)


async def get_billing(settings: Any) -> BillingPort:
    global _billing, _billing_built
    if not _billing_built:
        _billing = await build_billing(settings)
        _billing_built = True
    assert _billing is not None
    return _billing


async def reset_billing() -> None:
    """Drop the shared instance and close the platform reader (tests, shutdown)."""
    global _billing, _billing_built
    if _billing is not None:
        close = getattr(_billing, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to close billing reader: %s", exc)
    _billing = None
    _billing_built = False
