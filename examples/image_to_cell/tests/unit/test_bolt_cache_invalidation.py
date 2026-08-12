"""
Tests for B18 — invalidate the bolt_cap usage_cache at end of backfill.

The 30-second cache in bolt_cap protects against thundering-herd by
amortising the get_subscription_status RPC across concurrent row workers.
But during a backfill that consumes a meaningful chunk of the org's
budget, a sibling config (same worksheet, sequential in backfill_worksheet)
that runs ~10–20 seconds later would see the PRE-backfill consumed_bolts
value instead of the actual one — letting it skip past the budget gate
based on a stale snapshot.

invalidate_usage_cache(org_id) is a one-line drop that backfill_worksheet
calls in its finally clause. The next call to load_i2c_org_usage_context
re-queries Supabase and gets the post-backfill consumed counter.
"""

from __future__ import annotations

import time

from src.services import bolt_cap


def test_invalidate_usage_cache_removes_org_entry():
    """Direct unit test on the helper. Pre-populates the cache, calls
    invalidate, asserts the entry is gone."""
    org = "test-org-1"
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache[org] = {"ts": time.monotonic(), "data": {"consumed": 100}}

    assert org in bolt_cap._usage_cache
    bolt_cap.invalidate_usage_cache(org)
    assert org not in bolt_cap._usage_cache


def test_invalidate_usage_cache_is_idempotent():
    """Removing an org that isn't in the cache is a no-op (does not raise)."""
    org = "test-org-not-cached"
    bolt_cap.invalidate_usage_cache(org)  # should not raise
    bolt_cap.invalidate_usage_cache(org)  # second call also fine


def test_invalidate_only_affects_targeted_org():
    """Other orgs' cache entries are untouched."""
    org_a = "org-a"
    org_b = "org-b"
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache[org_a] = {"ts": time.monotonic(), "data": {"x": 1}}
        bolt_cap._usage_cache[org_b] = {"ts": time.monotonic(), "data": {"x": 2}}

    bolt_cap.invalidate_usage_cache(org_a)

    assert org_a not in bolt_cap._usage_cache
    assert org_b in bolt_cap._usage_cache
    # cleanup
    bolt_cap.invalidate_usage_cache(org_b)


def test_backfill_worksheet_invalidates_cache_in_finally(monkeypatch):
    """End-to-end: when backfill_worksheet runs (even if the configs list
    is empty), it should invalidate the cache for the org of the first
    config it found.

    We exercise the path where get_active_webhook_configs returns at least
    one config but the preflight blocks it — the finally clause must still
    fire and clear the cache."""
    from src.core.processor import ImageProcessor
    from unittest.mock import MagicMock

    # Pre-populate cache for an org
    org = "blocked-org-test"
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache[org] = {"ts": time.monotonic(), "data": {"x": 1}}

    # Build a minimal processor that returns 1 config and a blocked preflight
    fake_config = MagicMock()
    fake_config.organization_id = org
    fake_config.id = "config-1"
    fake_config.worksheet_id = 1

    fake_db = MagicMock()
    proc = ImageProcessor(supabase=fake_db)
    proc._config_repo = MagicMock()
    proc._config_repo.get_active_webhook_configs.return_value = [fake_config]

    # Block at the preflight so we never try to fetch the sheet
    monkeypatch.setattr(
        "src.core.processor.preflight_pause_if_blocked",
        lambda org_id, config_id: "plan_suspended",
    )

    import asyncio
    asyncio.run(proc.backfill_worksheet(worksheet_id=1))

    # finally clause should have invalidated
    assert org not in bolt_cap._usage_cache


def test_backfill_worksheet_invalidates_cache_when_no_configs(monkeypatch):
    """When there are no configs for the worksheet, backfill_worksheet
    returns early. invalidate_org_id was never set, so the finally block
    is a no-op for the cache. (Cache is left untouched, not corrupted.)"""
    from src.core.processor import ImageProcessor
    from unittest.mock import MagicMock

    org = "untouched-org-test"
    with bolt_cap._usage_cache_lock:
        bolt_cap._usage_cache[org] = {"ts": time.monotonic(), "data": {"x": 1}}

    fake_db = MagicMock()
    proc = ImageProcessor(supabase=fake_db)
    proc._config_repo = MagicMock()
    proc._config_repo.get_active_webhook_configs.return_value = []  # no configs

    import asyncio
    asyncio.run(proc.backfill_worksheet(worksheet_id=999))

    # Org was never identified, so its cache entry is untouched
    assert org in bolt_cap._usage_cache
    # cleanup
    bolt_cap.invalidate_usage_cache(org)
