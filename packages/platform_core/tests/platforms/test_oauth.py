"""Unit tests for smarthub_core.platforms.shared.oauth.

The module is mostly an interface + a single helper (``should_refresh``).
The interface itself is covered by impls (HubSpot, Slack, ... when added);
here we test the helper's edge cases since refresh decisions must be
deterministic across providers.
"""

from datetime import datetime, timedelta, timezone

from smarthub_core.platforms.shared.oauth import (
    DEFAULT_REFRESH_THRESHOLD,
    should_refresh,
)


class TestShouldRefresh:
    def test_none_expires_never_refresh(self):
        # Long-lived tokens (Smartsheet PAT, Stripe API key) report no expiry
        assert should_refresh(None) is False

    def test_well_within_lifetime_does_not_refresh(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        assert should_refresh(future) is False

    def test_within_threshold_refreshes(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=2)
        assert should_refresh(future) is True

    def test_expired_refreshes(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        assert should_refresh(past) is True

    def test_naive_datetime_treated_as_utc(self):
        future_naive = datetime.utcnow() + timedelta(hours=1)
        assert should_refresh(future_naive) is False

        soon_naive = datetime.utcnow() + timedelta(minutes=2)
        assert should_refresh(soon_naive) is True

    def test_custom_threshold(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert should_refresh(future) is False  # default 5min threshold
        assert (
            should_refresh(future, threshold=timedelta(minutes=15)) is True
        )

    def test_now_override_for_deterministic_tests(self):
        ref = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        expires = ref + timedelta(minutes=10)
        # 10 min ahead, default threshold 5 min → no refresh
        assert should_refresh(expires, now=ref) is False
        # 10 min ahead with threshold 11 min → refresh
        assert (
            should_refresh(expires, threshold=timedelta(minutes=11), now=ref)
            is True
        )

    def test_default_threshold_is_five_minutes(self):
        assert DEFAULT_REFRESH_THRESHOLD == timedelta(minutes=5)
