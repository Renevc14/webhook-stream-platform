"""SH-153 / SH-229 — publish gateway envelopes to per-tool Redis Streams.

The gateway's only durable side effect: after HMAC verification, XADD one
envelope per callback to ``stream:{stream_name}``. Stream names come from the
tool registry (SH-228). Unregistered slugs fail closed — no orphan streams.

Failure policy: a failed publish raises ``StreamPublishError`` and the route
returns 5xx, so the provider (Smartsheet) re-delivers the callback. Unknown
tool raises ``UnknownToolError`` → ingress 404 (retry will not help). Consumer
dedup (SH-152 envelope keys + SH-99 attachment keys) absorbs at-least-once
delivery for real XADD failures.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from gateway.tools.registry import ToolRegistry, UnknownToolError, load_tool_registry

log = logging.getLogger(__name__)


class StreamPublishError(Exception):
    """XADD failed after retries — the caller must surface a 5xx so the
    provider re-delivers."""


def stream_for_tool(
    tool_slug: str, registry: Optional[ToolRegistry] = None
) -> str:
    """Resolve ``stream:{name}`` from the registry. Raises ``UnknownToolError``."""
    reg = registry or load_tool_registry()
    return reg.require(tool_slug).stream_key()


class StreamPublisher:
    """Thin wrapper over ``RedisClient.stream_publish`` with envelope
    stringification (Redis Streams only accept string values)."""

    def __init__(
        self, redis_client, *, registry: Optional[ToolRegistry] = None
    ) -> None:
        self._redis = redis_client
        self._registry = registry or load_tool_registry()

    async def publish(self, tool_slug: str, envelope: Dict[str, Any]) -> str:
        # Fail closed before touching Redis when the slug is unknown.
        stream = stream_for_tool(tool_slug, self._registry)
        payload = _stringify(envelope)
        try:
            msg_id = await self._redis.stream_publish(stream, payload)
        except Exception as e:
            raise StreamPublishError(f"XADD {stream} crashed: {e}") from e
        if msg_id is None:
            raise StreamPublishError(f"XADD {stream} failed after retries")
        log.info(
            "webhook_gateway: published stream=%s msg=%s event=%s events=%s",
            stream,
            msg_id,
            envelope.get("event_id"),
            envelope.get("event_count"),
        )
        return msg_id


def _stringify(envelope: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in envelope.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, str):
            out[k] = v
        elif isinstance(v, (dict, list)):
            out[k] = json.dumps(v)
        else:
            out[k] = str(v)
    return out


# Re-export for route handlers that already import from publisher.
__all__ = [
    "StreamPublishError",
    "StreamPublisher",
    "UnknownToolError",
    "stream_for_tool",
]
