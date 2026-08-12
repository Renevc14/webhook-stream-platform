"""I2C billing adapter — thin port over ``smarthub_core.billing``.

``port.py`` builds the lifespan singleton (``BoltBilling`` / ``MockBilling``).
``bolt_cap.py`` remains for estimate/pause context loaders and the deprecated
``atomic_*`` helpers until callers fully migrate.

SH-222 seeded Redis from ``get_org_billing_usage_total``; this package now
also exposes the shared ``BillingPort`` used by ``RowProcessor``.
"""

from __future__ import annotations

from smarthub_core.billing import (
    DEFAULT_USAGE_RPC,
    BillingPort,
    BoltBilling,
    BoltGate,
    MockBilling,
    OrgBudget,
    PlatformBudgetReader,
    Quote,
    Reservation,
)

from src.adapters.billing.port import build_billing, get_billing, reset_billing

PRODUCT_SLUG = "smart-image-to-cell"

__all__ = [
    "DEFAULT_USAGE_RPC",
    "PRODUCT_SLUG",
    "BillingPort",
    "BoltBilling",
    "BoltGate",
    "MockBilling",
    "OrgBudget",
    "PlatformBudgetReader",
    "Quote",
    "Reservation",
    "build_billing",
    "get_billing",
    "reset_billing",
]
