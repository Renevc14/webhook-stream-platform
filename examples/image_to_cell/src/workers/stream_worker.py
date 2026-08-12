"""SH-152 — I2C embedded stream consumer for ``stream:i2c``.

``I2CStreamWorker`` subclasses ``smarthub_core.worker.SmarthubWorker`` and
runs inside the same process as the HTTP API when ``RUN_STREAM_CONSUMER=true``
(SH-151 runtime role). It consumes the versioned envelopes the Webhook
Gateway publishes (SH-153) and delegates to the existing ``WebhookHandler``
pipeline — the exact same code path the direct HTTP webhook route uses, so
SH-99 dedup, SH-110 row grouping, bolt-cap, usage_log idempotency, and the
Supabase DLQ all apply unchanged.

Envelope contract (gateway → stream:i2c), one entry per webhook POST:

    {v: "1", event_id, provider, tool, org_id, config_id?, webhook_id?,
     received_at, event_count, raw: <full original Smartsheet payload JSON>}

``raw`` is the untransformed Smartsheet callback body (architecture rule —
TARGET_ARCHITECTURE §Stream payload envelope), so the consumer rebuilds
``WebhookPayload`` with the same Pydantic model the HTTP route uses.

Failure semantics:
- non-smartsheet provider → skip + ack (filter per SH-152 scope)
- malformed raw → FatalError → stream DLQ (stream:i2c:dlq) + ack
- WebhookHandler returned success=False (whole callback blew up) →
  RetryableError → backoff retry, DLQ after max_attempts
- partial row failures inside the handler → already stored in the Supabase
  DLQ by the handler itself; the stream message is ACKed (the DLQ processor
  owns those retries — re-delivering the stream message would double-process
  the rows that succeeded).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from smarthub_core.worker.base import (
    FatalError,
    Job,
    Result,
    RetryableError,
    SmarthubWorker,
)

from src.models.webhook import WebhookPayload

log = logging.getLogger(__name__)

STREAM_NAME = "stream:i2c"
CONSUMER_GROUP = "i2c-workers"


class I2CStreamWorker(SmarthubWorker):
    """Consumes gateway envelopes from ``stream:i2c`` and runs them through
    the existing webhook processing pipeline."""

    tool_slug = "i2c"

    def __init__(self, redis, *, webhook_handler=None, pod_id=None) -> None:
        super().__init__(
            redis,
            pod_id=pod_id,
            stream=STREAM_NAME,
            group=CONSUMER_GROUP,
        )
        # Injected for tests; lazy default so importing this module does not
        # drag the full handler dependency tree into unit tests.
        self._webhook_handler = webhook_handler

    def _get_handler(self):
        if self._webhook_handler is None:
            from src.workers.webhook_handler import get_webhook_handler

            self._webhook_handler = get_webhook_handler()
        return self._webhook_handler

    def dedup_key(self, job: Job) -> Optional[str]:
        """Envelope-level dedup guards stream redelivery (consumer crash
        between process and ack). Attachment-level SH-99 dedup inside the
        handler covers Smartsheet's own HTTP retries, which arrive as new
        envelopes with fresh event_ids."""
        return f"dedup:i2c:envelope:{job.event_id}"

    async def process(self, job: Job) -> Result:
        provider = job.payload.get("provider", "")
        if provider and provider != "smartsheet":
            log.info(
                "[STREAM:I2C] skipping non-smartsheet envelope provider=%s event=%s",
                provider,
                job.event_id,
            )
            return Result(success=True, metadata={"skipped": "provider"})

        payload = self._parse_webhook_payload(job.raw)

        handler = self._get_handler()
        result = await handler.handle_webhook(payload)

        if not result.success:
            # The whole callback errored before row fan-out (config fetch,
            # grouping, etc.) — nothing was processed, safe to retry.
            raise RetryableError(
                f"webhook processing failed: {result.status}: {result.message}"
            )

        # Partial row failures are in the Supabase DLQ already (handler
        # stores them) — ack the stream message and let the DLQ processor
        # own the retries.
        return Result(
            success=True,
            metadata={
                "status": result.status,
                "events_processed": result.events_processed,
                "events_failed": result.events_failed,
                "attachments_processed": result.attachments_processed,
            },
        )

    @staticmethod
    def _parse_webhook_payload(raw: Any) -> WebhookPayload:
        """Rebuild the Smartsheet callback from the envelope's ``raw`` field.

        ``SmarthubWorker._parse_envelope`` already JSON-decodes ``raw`` when
        it is valid JSON; accept a dict or a JSON string, reject the rest.
        """
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                raise FatalError(f"raw is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise FatalError(
                f"raw must be a Smartsheet callback object, got {type(raw).__name__}"
            )
        try:
            return WebhookPayload.model_validate(raw)
        except Exception as e:
            raise FatalError(f"raw failed WebhookPayload validation: {e}") from e
