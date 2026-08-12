"""SH-212 — SmartQR stream consumer on top of ``SmarthubWorker``.

Replaces the hand-rolled XREADGROUP loop with the shared worker base so
SmartQR gets the same failure semantics as I2C: bounded retries with
exponential backoff, a real dead-letter stream (``stream:smart-qr:dlq``)
instead of messages piling up in the pending list, and structured envelope
parsing.

Envelope contract (gateway → ``stream:smart-qr``), one entry per callback:

    {v: "1", event_id, provider, tool, org_id, config_id?, received_at,
     event_count, raw: <original Smartsheet callback JSON>}

Two shapes are accepted:

* **Gateway envelope** — ``raw`` holds the Smartsheet body; every event is
  fanned out to the configs bound to that sheet.
* **Direct entry** (``org_id`` + ``config_id`` + ``row_id``, no ``v``) — the
  manual XADD used by the sandbox smoke scripts; kept working so operators
  can replay a single row.

Dedup: deliberately no envelope-level dedup key. Orphan recovery reclaims
pending entries after ``claim_idle_ms``, and a dedup key outliving that
window would make the reclaimed message ack without being processed — the
exact loss the reclaim exists to prevent. ``ProcessRowService`` already skips
unchanged rows by payload hash, so a replay is a no-op charge-wise.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from smarthub_core.worker.base import (
    FatalError,
    Job,
    RetryableError,
    Result,
    SmarthubWorker,
    WorkerError,
)

from smart_qr.config.constants import CHANGE_AGENT_ID, CONSUMER_GROUP, STREAM_NAME

log = logging.getLogger(__name__)

# Pending entries idle longer than this are assumed orphaned (pod crash/deploy).
CLAIM_IDLE_MS = 60_000


class SmartQRStreamWorker(SmarthubWorker):
    """Consumes gateway envelopes from ``stream:smart-qr``."""

    tool_slug = "smart-qr"
    claim_idle_ms = CLAIM_IDLE_MS

    def __init__(self, redis, *, processor=None, configs=None, pod_id=None) -> None:
        super().__init__(
            redis,
            pod_id=pod_id,
            stream=STREAM_NAME,
            group=CONSUMER_GROUP,
        )
        # Injected in tests; built lazily so importing this module stays cheap.
        self._processor = processor
        self._configs = configs

    # ----- dependencies ---------------------------------------------------

    def _get_configs(self):
        if self._configs is None:
            from smart_qr.adapters.db.repositories import ConfigRepository

            self._configs = ConfigRepository()
        return self._configs

    # ----- envelope -------------------------------------------------------

    def _parse_envelope(self, msg_id: str, payload: dict[str, str]) -> Job:
        if payload.get("v"):
            return super()._parse_envelope(msg_id, payload)

        # Direct entry (manual XADD): synthesize an envelope so the rest of
        # the pipeline sees a single shape.
        org = payload.get("org_id") or payload.get("organization_id")
        config_id = payload.get("config_id")
        row_id = payload.get("row_id")
        if not (org and config_id and row_id):
            raise WorkerError(
                "entry has no envelope version and is not a direct "
                "org_id/config_id/row_id entry"
            )
        return Job(
            msg_id=msg_id,
            event_id=f"direct-{msg_id}",
            tool=self.tool_slug,
            org_id=str(org),
            config_id=str(config_id),
            received_at=payload.get("received_at", ""),
            raw={"row_id": row_id},
            payload=payload,
        )

    # ----- processing -----------------------------------------------------

    async def process(self, job: Job) -> Result:
        provider = job.payload.get("provider", "")
        if provider and provider != "smartsheet":
            log.info(
                "[STREAM:SMART-QR] skipping non-smartsheet envelope provider=%s event=%s",
                provider,
                job.event_id,
            )
            return Result(success=True, metadata={"skipped": "provider"})

        raw = _as_dict(job.raw)
        org_id = job.org_id or str(raw.get("organization_id") or raw.get("org_id") or "")
        if not org_id:
            raise FatalError("envelope has no organization id")

        row_id = raw.get("row_id") or job.payload.get("row_id")
        if job.config_id and row_id:
            return await self._process_direct(org_id, job.config_id, row_id, raw)

        return await self._process_callback(org_id, raw)

    async def _process_direct(
        self, org_id: str, config_id: str, row_id: Any, raw: dict
    ) -> Result:
        await self._run(
            organization_id=UUID(org_id),
            config_id=UUID(str(config_id)),
            row_id=int(row_id),
            changed_column_ids=raw.get("changed_column_ids"),
            change_agent=raw.get("change_agent"),
            force=bool(raw.get("force", False)),
        )
        return Result(success=True, metadata={"rows_processed": 1, "mode": "direct"})

    async def _process_callback(self, org_id: str, raw: dict) -> Result:
        worksheet_id = raw.get("scopeObjectId")
        if not worksheet_id:
            log.info("[STREAM:SMART-QR] envelope has no scopeObjectId — skipping")
            return Result(success=True, metadata={"skipped": "no_scope_object_id"})

        org_uuid = UUID(org_id)
        try:
            configs = await self._get_configs().list_for_worksheet(
                org_uuid, int(worksheet_id)
            )
        except Exception as e:
            raise RetryableError(f"config lookup failed: {e}") from e

        if not configs:
            log.info(
                "[STREAM:SMART-QR] no active config for org=%s sheet=%s — skipping",
                org_id,
                worksheet_id,
            )
            return Result(success=True, metadata={"skipped": "no_config"})

        processed = 0
        skipped_self = 0
        for event in raw.get("events") or []:
            if not isinstance(event, dict):
                continue
            agent = str(event.get("changeAgent") or "")
            if CHANGE_AGENT_ID in agent:
                # Our own write coming back — processing it would loop.
                skipped_self += 1
                continue
            row_id = event.get("rowId")
            if row_id is None:
                continue
            column_id = event.get("columnId")
            changed = [int(column_id)] if column_id is not None else None
            for config in configs:
                await self._run(
                    organization_id=org_uuid,
                    config_id=config.id,
                    row_id=int(row_id),
                    changed_column_ids=changed,
                    change_agent=agent or None,
                )
                processed += 1

        return Result(
            success=True,
            metadata={
                "rows_processed": processed,
                "self_events_skipped": skipped_self,
                "configs": len(configs),
            },
        )

    async def _run(self, **kwargs) -> None:
        """Run one row through ProcessRowService, mapping failures to retries.

        ``ProcessRowService`` returns typed outcomes instead of raising for most
        paths. Status ``error`` must still drive the shared worker retry/DLQ
        loop — otherwise a failed upload is ACKed and never retried.
        """
        processor = self._processor
        if processor is None:
            from smart_qr.workers.stream_consumer import build_processor

            processor = self._processor = await build_processor()
        try:
            result = await processor.process(**kwargs)
        except Exception as e:
            # Row-level failures are transient far more often than not
            # (Smartsheet 5xx, token refresh, Supabase blips). The base class
            # retries with backoff and DLQs after max_attempts.
            raise RetryableError(f"row processing failed: {e}") from e

        if result is None or getattr(result, "status", None) != "error":
            return

        reason = str(getattr(result, "reason", None) or "unknown")
        # Permanent: missing config / revoked OAuth — retrying cannot help.
        if reason in {"config_not_found", "connection_missing"} or reason.startswith(
            "reconnect_required"
        ):
            raise FatalError(f"row processing failed: {reason}")
        raise RetryableError(f"row processing failed: {reason}")


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FatalError(f"raw is not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise FatalError(f"raw must be an object, got {type(parsed).__name__}")
        return parsed
    return {}
