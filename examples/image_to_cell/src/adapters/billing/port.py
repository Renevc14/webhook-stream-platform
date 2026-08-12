"""Billing port: reserve before work, release on failure (I2C adapter).

Thin Image-to-Cell adapter over ``smarthub_core.billing``. Construction reuses
the lifespan-managed Redis client (``get_redis_client``) so bolt-cap keys share
one connection pool with webhook dedup and the Smartsheet rate limiter.
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
    """Real metering against the Hub plan; fail-closed outside local."""
    from smarthub_core.billing import BoltGate, PlatformBudgetReader

    from src.services.redis_client import get_redis_client

    if not (settings.supabase_url and settings.supabase_service_role_key):
        log.error(
            "I2C billing: SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY missing — "
            "the plan cannot be read, so every request will be refused"
        )

    reader = PlatformBudgetReader(
        supabase_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        product_slug=getattr(settings, "product_slug", None) or "smart-image-to-cell",
        usage_rpc=DEFAULT_USAGE_RPC,
    )
    redis = get_redis_client()
    is_local = bool(getattr(settings, "is_local", False))
    if redis is None and not is_local:
        log.error(
            "I2C billing: Redis not wired outside local — reserves will be denied "
            "(fail-closed; no legacy peek)"
        )
    gate = BoltGate(
        reader=reader,
        redis=redis,
        allow_when_unknown=is_local,
    )
    log.info(
        "Bolt metering enabled for product_slug=%s usage_rpc=%s is_local=%s redis=%s",
        getattr(settings, "product_slug", None) or "smart-image-to-cell",
        DEFAULT_USAGE_RPC,
        is_local,
        "wired" if redis is not None else "missing",
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
