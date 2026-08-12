"""Stream envelope builder (SH-153 XADDs this shape).

One envelope per webhook callback — NOT per event. The I2C worker pipeline
(SH-110) groups events by row and resolves attachment parent rows across the
whole callback; splitting per event would lose `scopeObjectId` and defeat
that grouping. `raw` is therefore the provider's original callback body,
untransformed, per TARGET_ARCHITECTURE §Stream payload envelope.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from smarthub_core.platforms.shared.webhook_base import NormalizedEvent

# String to match smarthub_core.worker.base.ENVELOPE_VERSION — the consumer
# rejects envelopes whose `v` differs.
ENVELOPE_VERSION = "1"


def build_callback_envelope(
    *,
    provider: str,
    tool_slug: str,
    org_id: str,
    body: bytes,
    events: List[NormalizedEvent],
    webhook_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the versioned stream payload for one webhook callback."""
    return {
        "v": ENVELOPE_VERSION,
        "event_id": str(uuid.uuid4()),
        "provider": provider,
        "tool": tool_slug,
        "org_id": org_id,
        "webhook_id": webhook_id or "",
        "event_count": len(events),
        "event_types": ",".join(sorted({e.event_type for e in events})),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw": body.decode("utf-8"),
    }


def build_envelopes(
    *,
    provider: str,
    tool_slug: str,
    org_id: str,
    events: List[NormalizedEvent],
    webhook_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Per-event envelopes — kept for future providers whose events are
    independent (no cross-event grouping). Smartsheet uses
    ``build_callback_envelope`` instead."""
    received_at = datetime.now(timezone.utc).isoformat()
    out: List[Dict[str, Any]] = []
    for event in events:
        out.append(
            {
                "v": ENVELOPE_VERSION,
                "event_id": str(uuid.uuid4()),
                "provider": provider,
                "tool": tool_slug,
                "org_id": org_id,
                "webhook_id": webhook_id,
                "event_type": event.event_type,
                "external_id": event.external_id,
                "occurred_at": event.occurred_at,
                "received_at": received_at,
                "raw": json.dumps(event.raw) if not isinstance(event.raw, str) else event.raw,
            }
        )
    return out
