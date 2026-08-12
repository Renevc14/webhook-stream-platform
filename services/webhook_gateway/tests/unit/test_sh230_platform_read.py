"""SH-230 Phase C — secret sync reads platform_webhook_registrations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from gateway.lifecycle.platform_registrations import PLATFORM_WEBHOOKS_TABLE
from gateway.lifecycle.secret_sync import (
    SecretCacheSync,
    WebhookSecretSource,
    default_secret_sources,
)
from gateway.secrets.store import InMemoryWebhookSecretStore
from gateway.tools.registry import load_tool_registry


class FakeSupabase:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self.tables = tables
        self._name: Optional[str] = None
        self._filters: Dict[str, Any] = {}
        self._select_cols: Optional[str] = None

    def table(self, name: str):
        self._name = name
        self._filters = {}
        self._select_cols = None
        self.tables.setdefault(name, [])
        return self

    def select(self, cols):
        self._select_cols = cols
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def execute(self):
        assert self._name is not None
        rows = self.tables[self._name]
        hits = []
        for row in rows:
            if all(str(row.get(k)) == str(v) for k, v in self._filters.items()):
                hits.append(row)
        return SimpleNamespace(data=hits)

    def execute_with_retry(self, operation, description="operation", **_k):
        return operation()


@pytest.fixture
def crypto():
    from smarthub_core.crypto import CryptoService
    import base64

    return CryptoService(base64.b64encode(b"0" * 32).decode())


def test_default_sources_prefer_platform_when_flag_set():
    sources = default_secret_sources(
        registry=load_tool_registry(),
        prefer_platform_table=True,
    )
    assert sources
    for s in sources:
        assert s.table == PLATFORM_WEBHOOKS_TABLE
        assert s.secret_column == "shared_secret"
        assert s.prefer_platform_table is True


def test_default_sources_use_tool_tables_by_default():
    sources = default_secret_sources(registry=load_tool_registry())
    by_slug = {s.tool_slug: s for s in sources}
    assert by_slug["smart-image-to-cell"].table == "imagetocell_webhooks"
    assert by_slug["smart-qr"].table == "smartqr_webhooks"
    assert by_slug["smart-qr"].secret_column == "shared_secret_encrypted"


@pytest.mark.asyncio
async def test_platform_read_filters_by_provider_and_tool(crypto):
    plaintext = "s3cret"
    cipher = crypto.encrypt(plaintext)
    sb = FakeSupabase(
        {
            PLATFORM_WEBHOOKS_TABLE: [
                {
                    "organization_id": "org-1",
                    "webhook_id": 101,
                    "shared_secret": cipher,
                    "status": "active",
                    "provider": "smartsheet",
                    "tool_slug": "smart-image-to-cell",
                },
                {
                    "organization_id": "org-1",
                    "webhook_id": 202,
                    "shared_secret": cipher,
                    "status": "active",
                    "provider": "smartsheet",
                    "tool_slug": "smart-qr",
                },
            ]
        }
    )
    store = InMemoryWebhookSecretStore()
    source = WebhookSecretSource(
        tool_slug="smart-image-to-cell",
        table=PLATFORM_WEBHOOKS_TABLE,
        secret_column="shared_secret",
        provider="smartsheet",
        prefer_platform_table=True,
    )
    sync = SecretCacheSync(
        supabase=sb,
        secret_store=store,
        crypto=crypto,
        sources=[source],
    )
    result = await sync.refresh_all()
    assert result.secrets_cached == 1
    secrets = await store.get_secrets("org-1", "smart-image-to-cell")
    assert secrets == {"101": plaintext}
    assert "202" not in secrets
