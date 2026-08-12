"""Canonical Redis key names for org-wide bolt metering.

Bolts are one budget per organization. Every tool that enforces a cap must use
these helpers so Image-to-Cell, SmartQR and future tools share the same
``bolt-cap:{org}:*`` counters. Tool-scoped namespaces would let an org spend
its allocation once per tool.

Hold keys (``bolt-hold:...``) are *metadata* for the exact bolts reserved by one
operation. Their TTL only expires the metadata — it does **not** decrement
``bolt-cap:{org}:consumed``. Callers must ``release`` (or a future reconciler
must safely reverse the aggregate) when work fails.
"""

from __future__ import annotations

from typing import Union
from uuid import UUID

OrgId = Union[str, UUID]

# Shared with every tool: one budget per organization.
CONSUMED_KEY = "bolt-cap:{org}:consumed"
LIMIT_KEY = "bolt-cap:{org}:limit"
CYCLE_KEY = "bolt-cap:{org}:cycle_id"
HOLD_KEY = "bolt-hold:{org}:{operation_id}"

LIMIT_TTL_S = 60
# Hold metadata TTL. Expiry does not auto-refund the consumed counter.
HOLD_TTL_S = 1800


def _org(org_id: OrgId) -> str:
    return str(org_id)


def consumed_key(org_id: OrgId) -> str:
    return CONSUMED_KEY.format(org=_org(org_id))


def limit_key(org_id: OrgId) -> str:
    return LIMIT_KEY.format(org=_org(org_id))


def cycle_id_key(org_id: OrgId) -> str:
    return CYCLE_KEY.format(org=_org(org_id))


def hold_key(org_id: OrgId, operation_id: str) -> str:
    return HOLD_KEY.format(org=_org(org_id), operation_id=operation_id)
