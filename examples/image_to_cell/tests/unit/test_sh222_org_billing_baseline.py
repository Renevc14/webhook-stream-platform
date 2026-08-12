"""SH-222 — I2C bolt baseline uses org-wide ``get_org_billing_usage_total``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


class _FakeRpc:
    def __init__(self, data: Any):
        self._data = data

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeTable:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._filters: Dict[str, Any] = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        hits = [
            r
            for r in self._rows
            if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
        ]
        return SimpleNamespace(data=hits)


class _FakeDb:
    def __init__(
        self,
        *,
        usage_total: Any = 120,
        usage_raises: bool = False,
        allocated: int = 1000,
        sub_consumed: int = 10,
        hard_cap_bolt_amount: Optional[int] = None,
        plan_type: str = "team",
        bolts_per_image: int = 15,
    ):
        self._usage_total = usage_total
        self._usage_raises = usage_raises
        self._sub = {
            "current_cycle_total_bolts_allocated": allocated,
            "current_cycle_total_bolts_consumed": sub_consumed,
            "current_cycle_start": "2026-07-01T00:00:00Z",
            "hard_cap_reached": False,
            "hard_cap_bolt_amount": hard_cap_bolt_amount,
        }
        self._plan_type = plan_type
        self._bolts_per_image = bolts_per_image
        self.client = MagicMock()
        self.client.rpc.side_effect = self._rpc

    def _rpc(self, name: str, params: Dict[str, Any]):
        if name == "get_subscription_status":
            return _FakeRpc([self._sub])
        if name == "get_org_billing_usage_total":
            if self._usage_raises:
                raise RuntimeError("rpc down")
            return _FakeRpc(self._usage_total)
        raise AssertionError(name)

    def table(self, name: str):
        if name == "organization_subscriptions":
            return _FakeTable(
                [
                    {
                        "organization_id": "org-1",
                        "hard_cap_percentage": None,
                        "plan_suspended": False,
                        "bolts_pending_payment": False,
                    }
                ]
            )
        if name == "organizations":
            return _FakeTable([{"id": "org-1", "plan_type": self._plan_type}])
        if name == "pricing_list":
            return _FakeTable(
                [
                    {
                        "product_slug": "smart-image-to-cell",
                        "primary_event_bolts": self._bolts_per_image,
                    }
                ]
            )
        if name == "imagetocell_usage_log":
            # Would previously seed a different baseline — must be ignored.
            return _FakeTable([])
        raise AssertionError(name)


@pytest.fixture(autouse=True)
def _clear_usage_cache():
    from src.services import bolt_cap

    bolt_cap._usage_cache.clear()
    yield
    bolt_cap._usage_cache.clear()


def test_usage_context_seeds_from_org_billing_rpc_not_event_log():
    from src.services.bolt_cap import _load_i2c_org_usage_context_inner

    db = _FakeDb(usage_total=240, sub_consumed=10)
    with patch("src.services.bolt_cap.get_supabase_service", return_value=db):
        ctx = _load_i2c_org_usage_context_inner("org-1")

    assert ctx is not None
    assert ctx["consumed_bolts_start"] == 240
    assert ctx["bolts_per_image"] == 15
    assert ctx["allocated_bolts"] == 1000


def test_usage_context_prefers_hard_cap_bolt_amount():
    from src.services.bolt_cap import _load_i2c_org_usage_context_inner

    db = _FakeDb(usage_total=50, hard_cap_bolt_amount=800)
    with patch("src.services.bolt_cap.get_supabase_service", return_value=db):
        ctx = _load_i2c_org_usage_context_inner("org-1")

    assert ctx is not None
    assert ctx["ceiling_bolts"] == 800


def test_usage_rpc_failure_falls_back_to_subscription_consumed():
    from src.services.bolt_cap import _load_i2c_org_usage_context_inner

    db = _FakeDb(usage_raises=True, sub_consumed=42)
    with patch("src.services.bolt_cap.get_supabase_service", return_value=db):
        ctx = _load_i2c_org_usage_context_inner("org-1")

    assert ctx is not None
    assert ctx["consumed_bolts_start"] == 42


def test_org_billing_usage_total_parses_scalar_and_dict():
    from src.services.bolt_cap import _org_billing_usage_total

    db = _FakeDb(usage_total=99)
    assert _org_billing_usage_total(db, "org-1") == 99

    db2 = _FakeDb(usage_total=[{"total": 77}])
    assert _org_billing_usage_total(db2, "org-1") == 77
