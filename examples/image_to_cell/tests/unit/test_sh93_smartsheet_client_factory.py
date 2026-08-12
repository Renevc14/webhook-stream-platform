"""SH-93 — ``get_smartsheet_client`` factory.

Pins the new factory's contract without exercising the smarthub_core client
internals (those have their own tests in the smarthub_core repo). What this
file guards:

* When no ``RedisClient`` is wired (i.e. lifespan failed to start one), the
  factory raises ``SmartsheetError`` with a clear message — callers know to
  fall back to the legacy ``get_smartsheet_for_organization`` or surface the
  problem.
* When a ``RedisClient`` is wired, the factory returns a fresh
  ``SmartsheetClient`` scoped to the right org + product_slug, parameterised
  with the i2c-3 rate-limit ceiling from settings.
* The legacy factory ``get_smartsheet_for_organization`` is left intact —
  this ticket is additive; SH-100 migrates wrappers to the new client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.smartsheet.client import (
    SmartsheetError,
    get_smartsheet_client,
)
from src.services.redis_client import set_redis_client


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """Each test starts with no Redis installed so the no-Redis path is
    deterministic; the test cleans up afterwards too."""
    set_redis_client(None)
    yield
    set_redis_client(None)


class TestNoRedisInstalled:
    def test_raises_clear_error(self):
        with pytest.raises(SmartsheetError) as excinfo:
            get_smartsheet_client("any-org")
        msg = str(excinfo.value)
        assert "Redis" in msg
        # SH-102 message references the removed legacy fallback so an
        # operator hitting this error in the field knows there is no
        # alternative path to fall back to.
        assert "no legacy fallback" in msg


class TestRedisInstalled:
    def test_returns_smartsheet_client_scoped_to_org_and_slug(self):
        from smarthub_core.platforms.smartsheet import SmartsheetClient

        set_redis_client(MagicMock())  # client doesn't have to be live

        client = get_smartsheet_client("org-123")

        assert isinstance(client, SmartsheetClient)
        assert client.org_id == "org-123"
        # Product slug comes from i2c-3 Settings (default: smart-image-to-cell).
        assert client.product_slug == "smart-image-to-cell"

    def test_independent_orgs_get_independent_clients(self):
        set_redis_client(MagicMock())

        c1 = get_smartsheet_client("org-1")
        c2 = get_smartsheet_client("org-2")

        # Cheap to construct: factory returns a fresh instance per call.
        # State that matters (token cache + rate-limit window) lives in
        # Redis under stable keys, so two instances for the same org would
        # share that state — we don't need to deduplicate at the
        # constructor level.
        assert c1 is not c2
        assert c1.org_id == "org-1"
        assert c2.org_id == "org-2"


class TestLegacyPathRemoved:
    """SH-102: ``get_smartsheet_for_organization`` was deleted.

    Pin the absence so any future "let's keep the legacy fallback
    around as a safety net" attempt has to update this test and read
    the SH-102 rationale at the same time.
    """

    def test_legacy_factory_no_longer_importable(self):
        from src.services.smartsheet import client

        assert not hasattr(client, "get_smartsheet_for_organization")
        assert not hasattr(client, "SmartsheetService")
