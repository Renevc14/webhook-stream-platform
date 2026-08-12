"""
Webhook Routes
==============

Endpoints for Smartsheet webhook handling.

Based on v2 robustness patterns for challenge handling.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import APIRouter, Depends, Request, Response, HTTPException, status, Header
from starlette.requests import ClientDisconnect

from src.config.settings import get_settings

from src.api.dependencies import get_webhook_handler_dep
from src.workers.webhook_handler import WebhookHandler
from src.models.webhook import WebhookPayload
from src.utils.logging import FlowLogger, log_context
from src.config.constants import TABLE_NAMES
from src.services.supabase.client import get_supabase_service


log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhooks"])


@router.post("/smartsheet")
async def handle_smartsheet_webhook(
    request: Request,
    smartsheet_hmac_sha256: Optional[str] = Header(None, alias="Smartsheet-Hmac-SHA256"),
) -> Any:
    """
    Handle Smartsheet webhook callback.

    This endpoint receives real-time notifications from Smartsheet
    when attachments are added to a monitored worksheet.

    Based on v2 robustness patterns for challenge handling:
    - Responds IMMEDIATELY to challenge (before reading body)
    - Responds with BOTH header AND JSON body
    - Handler dependency is resolved AFTER challenge check to avoid delay
    """
    # Log incoming request
    log.info(f"[WEBHOOK:INCOMING] Received POST from {request.client.host if request.client else 'unknown'}")

    # CRITICAL: Check for verification challenge in HEADERS first
    # Smartsheet sends the challenge in 'Smartsheet-Hook-Challenge' header (case-insensitive)
    challenge_header = (
        request.headers.get("Smartsheet-Hook-Challenge")
        or request.headers.get("smartsheet-hook-challenge")
        or request.headers.get("X-Smartsheet-Hook-Challenge")
    )

    # CRITICAL FIX: If challenge header is present, respond IMMEDIATELY
    # Smartsheet has a very short timeout and may disconnect before we read the body
    # We don't need the body to respond to the challenge - just echo the header value
    if challenge_header:
        # Strip any whitespace from challenge value
        challenge_value = challenge_header.strip()

        log.info(f"[WEBHOOK:VERIFY] ========== CHALLENGE VERIFICATION ==========")
        log.info(f"[WEBHOOK:VERIFY] Challenge value: {challenge_value}")
        log.info(f"[WEBHOOK:VERIFY] Challenge length: {len(challenge_value)}")

        response_body = json.dumps({'smartsheetHookResponse': challenge_value})

        log.info(f"[WEBHOOK:VERIFY] Response body: {response_body}")
        log.info(f"[WEBHOOK:VERIFY] Response header: Smartsheet-Hook-Response = {challenge_value}")
        log.info(f"[WEBHOOK:VERIFY] Sending 200 OK response NOW")

        # Create response matching v2 pattern exactly
        # Explicitly set both media_type AND Content-Type header for maximum compatibility
        response = Response(
            content=response_body,
            media_type="application/json",
            status_code=200,
            headers={
                'Smartsheet-Hook-Response': challenge_value,
                'Content-Type': 'application/json',
            }
        )

        log.info(f"[WEBHOOK:VERIFY] Response headers: {dict(response.headers)}")
        return response

    # No challenge header - this is a regular webhook event, read body
    try:
        body = await request.body()
    except ClientDisconnect:
        log.info("[WEBHOOK:EVENT] Client disconnected during body read")
        return Response(status_code=200)

    body_text = body.decode() if body else ""
    log.info(f"[WEBHOOK:INCOMING] Body length: {len(body_text)} bytes")

    # Opt-in raw body logging for investigations. Driven by
    # I2C_LOG_WEBHOOK_BODY in .env / task definition (read via Settings, so
    # the value comes from .env even though python-dotenv is not used at
    # the process level). OFF by default — bodies are large and contain
    # customer-scoped IDs.
    if get_settings().log_webhook_body and body_text:
        log.info(f"[WEBHOOK:INCOMING:BODY] {body_text}")

    # Handle empty body (initial verification callback without challenge header)
    if not body or body.strip() == b"":
        log.info("[WEBHOOK:INCOMING] Empty body received - initial callback verification")
        return {"status": "ok"}

    # At this point we have a body but no challenge - this is a webhook event
    log.info(f"[WEBHOOK:EVENT] Processing webhook event")

    # Parse payload to get worksheet_id
    try:
        payload_data = json.loads(body_text)
    except Exception as e:
        log.error(f"[WEBHOOK:ERROR] Failed to parse payload: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    # Extract worksheet_id from scopeObjectId
    worksheet_id = payload_data.get("scopeObjectId")
    webhook_id = payload_data.get("webhookId", "")
    all_events = payload_data.get("events", [])

    # Create flow logger for this webhook
    flow = FlowLogger(log, "WEBHOOK:RECEIVE", sheet=worksheet_id, webhook=webhook_id)
    flow.start(f"Received {len(all_events)} events")

    # Breakdown of what arrived: (objectType, eventType) -> count.
    # Cheap to compute and far more useful than logging the full body for
    # day-to-day debugging.
    breakdown = Counter(
        (e.get("objectType", "unknown"), e.get("eventType", "unknown"))
        for e in all_events
    )
    breakdown_str = ", ".join(
        f"{ot}.{et}={n}" for (ot, et), n in sorted(breakdown.items())
    )
    log.info(
        f"[WEBHOOK:RECEIVE] (sheet={worksheet_id}) Event breakdown: {breakdown_str}"
    )

    # Filter to only attachment creation events
    attachment_events = [
        e for e in all_events
        if e.get("objectType") == "attachment"
        and e.get("eventType") in ("created", "objectCreated")
    ]

    flow.step(1, f"Found {len(attachment_events)} attachment events")

    # Visibility: log non-attachment objectTypes seen so we know if Smartsheet
    # starts sending types we have not catalogued (e.g. proof, reminder,
    # workspace). They are ignored either way — this is a heads-up signal.
    known_object_types = {
        "attachment", "row", "cell", "column", "sheet",
        "discussion", "proof", "reminder", "comment",
        "workspace", "formAccessKey",
    }
    unknown_types = {
        e.get("objectType")
        for e in all_events
        if e.get("objectType") not in known_object_types and e.get("objectType") is not None
    }
    if unknown_types:
        log.info(
            f"[WEBHOOK:RECEIVE] (sheet={worksheet_id}) Unknown objectTypes seen and ignored: "
            f"{sorted(unknown_types)}. Update known_object_types if these recur."
        )

    if not worksheet_id:
        flow.failure("Missing scopeObjectId in payload")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing scopeObjectId in payload",
        )

    # Convert to int
    try:
        worksheet_id = int(worksheet_id)
    except (ValueError, TypeError):
        flow.failure(f"Invalid worksheet_id: {worksheet_id}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid scopeObjectId",
        )

    # CRITICAL: Get handler AFTER challenge check to avoid initialization delay
    # V3 WebhookHandler.__init__ creates repos and thread pools which can delay response
    # This ensures challenge responses are sent IMMEDIATELY before any slow initialization
    handler = get_webhook_handler_dep()

    # Verify signature if present
    # Based on v2 pattern: pass webhook_id for multi-method lookup
    flow.step(2, "Verifying signature")
    if smartsheet_hmac_sha256:
        verification = await handler.verify_signature(
            body,
            smartsheet_hmac_sha256,
            worksheet_id,
            webhook_id=webhook_id,
        )

        if not verification:
            # Handle different verification failure states (based on v2)
            status_code = status.HTTP_401_UNAUTHORIZED
            detail = "Invalid webhook signature"

            if hasattr(verification, 'status'):
                if verification.status == "zombie_webhook":
                    flow.warning(f"Zombie webhook detected: {verification.message}")
                    # Return 200 OK to stop Smartsheet from retrying zombie events
                    return {"status": "ignored", "reason": "zombie_webhook"}
                elif verification.status == "superseded_webhook_event":
                    flow.warning(f"Superseded webhook: {verification.message}")
                    # Return 200 OK to stop retries for old webhook events
                    return {"status": "ignored", "reason": "superseded_webhook"}
                elif verification.status == "no_secret":
                    flow.warning(f"No secret found: {verification.message}")
                    detail = "Webhook secret not found"
                else:
                    detail = verification.message or detail

            flow.failure(f"Signature verification failed: {detail}")
            raise HTTPException(
                status_code=status_code,
                detail=detail,
            )
    else:
        flow.debug("No signature provided - skipping verification")

    # Build payload from the FILTERED attachment_events.
    #
    # Smartsheet batches all sheet activity into a single webhook callback,
    # so the raw events list can include discussion/proof/reminder/etc. types
    # that we never act on. Passing the raw list to WebhookPayload caused
    # Pydantic validation crashes in production (incident 2026-05-15) and
    # dropped the entire payload — including the valid attachment events.
    # We pass only the events we actually care about. The model is also
    # permissive on objectType/eventType (see src/models/webhook.py) so even
    # a future code path that accidentally passes raw events does not crash.
    try:
        payload = WebhookPayload(
            webhook_id=webhook_id,
            worksheet_id=worksheet_id,
            scope=payload_data.get("scope", "sheet"),
            scope_object_id=worksheet_id,
            events=attachment_events,
        )
    except Exception as e:
        flow.failure("Invalid webhook payload structure", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload structure",
        )

    # Process webhook in background - respond immediately to prevent Smartsheet timeout
    import asyncio

    async def process_in_background():
        try:
            result = await handler.handle_webhook(payload)
            log.info(
                f"[WEBHOOK:BACKGROUND] Completed: {result.events_processed} processed, "
                f"{result.events_skipped} skipped, {result.events_failed} failed"
            )
        except Exception as e:
            log.error(f"[WEBHOOK:BACKGROUND] Failed: {e}")

    # Start background processing
    asyncio.create_task(process_in_background())

    flow.success(
        "Webhook acknowledged",
        events=len(attachment_events),
    )

    # Return immediately - Smartsheet requires response within ~30 seconds
    return {
        "status": "accepted",
        "events_received": len(all_events),
        "attachment_events": len(attachment_events),
        "message": "Processing in background",
    }


@router.get("/status")
async def webhook_status(
    handler: WebhookHandler = Depends(get_webhook_handler_dep),
) -> Dict[str, Any]:
    """Get webhook handler status."""
    return {
        "handler_running": handler.is_running,
        "status": "ready",
    }
