"""Unit tests for webhook gateway routes."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from gateway.app import create_app
from gateway.secrets.store import InMemoryWebhookSecretStore, secret_cache_key
from gateway.streams.publisher import StreamPublisher, StreamPublishError, stream_for_tool

SECRET = "test-shared-secret-1234567890"
ORG = "org-abc"
TOOL = "smart-image-to-cell"
WEBHOOK_ID = "999888777"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _secret_cache_key(org_id: str, tool_slug: str) -> str:
    return secret_cache_key(org_id, tool_slug)


class FakeStreamPublisher(StreamPublisher):
    """Records publishes in memory; optionally fails."""

    def __init__(self, fail: bool = False) -> None:
        self.published = []  # list of (stream, envelope)
        self._fail = fail

    async def publish(self, tool_slug: str, envelope: dict) -> str:
        if self._fail:
            raise StreamPublishError("XADD failed (fake)")
        self.published.append((stream_for_tool(tool_slug), envelope))
        return f"{len(self.published)}-0"


@pytest.fixture
def secret_store():
    return InMemoryWebhookSecretStore(
        {
            _secret_cache_key(ORG, TOOL): {WEBHOOK_ID: SECRET},
        }
    )


@pytest.fixture
def publisher():
    return FakeStreamPublisher()


@pytest.fixture
def app(secret_store, publisher):
    return create_app(secret_store=secret_store, publisher=publisher)


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "webhook-gateway"


@pytest.mark.asyncio
async def test_ready_without_redis(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_smartsheet_challenge(client):
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        headers={"Smartsheet-Hook-Challenge": "challenge-token-xyz"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("Smartsheet-Hook-Response") == "challenge-token-xyz"
    assert resp.content == b"challenge-token-xyz"


@pytest.mark.asyncio
async def test_smartsheet_signed_event_accepted(client, publisher):
    body = json.dumps(
        {
            "scopeObjectId": 123,
            "events": [
                {
                    "id": 1,
                    "objectType": "attachment",
                    "eventType": "created",
                    "timestamp": "2026-07-06T12:00:00Z",
                }
            ],
        }
    ).encode()
    sig = _sign(body)
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Smartsheet-Hmac-SHA256": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    # SH-153: exactly one stream entry per callback, on stream:i2c.
    assert len(publisher.published) == 1
    stream, envelope = publisher.published[0]
    assert stream == "stream:i2c"
    assert envelope["v"] == "1"
    assert envelope["provider"] == "smartsheet"
    assert envelope["tool"] == TOOL
    assert envelope["org_id"] == ORG
    assert envelope["webhook_id"] == WEBHOOK_ID
    assert envelope["event_count"] == 1
    # raw round-trips as the exact original body.
    assert json.loads(envelope["raw"]) == json.loads(body.decode())


@pytest.mark.asyncio
async def test_multi_event_callback_is_one_stream_entry(client, publisher):
    """SH-110 grouping happens in the consumer — the gateway must NOT split
    a batched callback into per-event entries."""
    body = json.dumps(
        {
            "scopeObjectId": 123,
            "events": [
                {"id": 1, "objectType": "attachment", "eventType": "created"},
                {"id": 2, "objectType": "attachment", "eventType": "created"},
                {"id": 3, "objectType": "row", "eventType": "updated"},
            ],
        }
    ).encode()
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={"Smartsheet-Hmac-SHA256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["events"] == 3
    assert len(publisher.published) == 1
    _, envelope = publisher.published[0]
    assert envelope["event_count"] == 3


@pytest.mark.asyncio
async def test_no_actionable_events_skips_stream(client, publisher):
    body = json.dumps({"scopeObjectId": 123, "events": []}).encode()
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={"Smartsheet-Hmac-SHA256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["events"] == 0
    assert publisher.published == []


@pytest.mark.asyncio
async def test_publish_failure_returns_503(secret_store):
    app = create_app(
        secret_store=secret_store, publisher=FakeStreamPublisher(fail=True)
    )
    transport = ASGITransport(app=app)
    body = json.dumps(
        {
            "scopeObjectId": 123,
            "events": [
                {"id": 1, "objectType": "attachment", "eventType": "created"}
            ],
        }
    ).encode()
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/webhook/smartsheet/{TOOL}/{ORG}",
            content=body,
            headers={"Smartsheet-Hmac-SHA256": _sign(body)},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_challenge_does_not_touch_stream(client, publisher):
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        headers={"Smartsheet-Hook-Challenge": "tok"},
    )
    assert resp.status_code == 200
    assert publisher.published == []


@pytest.mark.asyncio
async def test_unknown_provider_404(client):
    resp = await client.post(
        f"/webhook/hubspot/{TOOL}/{ORG}",
        content=b"{}",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_invalid_signature_401(client):
    body = b'{"events":[]}'
    resp = await client.post(
        f"/webhook/smartsheet/{TOOL}/{ORG}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Smartsheet-Hmac-SHA256": "deadbeef" * 8,
        },
    )
    assert resp.status_code == 401
