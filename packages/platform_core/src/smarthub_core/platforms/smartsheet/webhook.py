"""Smartsheet webhook handler — HMAC SHA-256 + sync challenge.

Implements ``BaseWebhookHandler`` for Smartsheet. Smartsheet's webhook protocol
has two specifics that this module encapsulates:

1. **Challenge handshake.** When a webhook is created/enabled, Smartsheet sends
   a request with the ``Smartsheet-Hook-Challenge`` header. The handler must
   respond 200 OK within 5 seconds, echoing the challenge value back both as
   the response body and in the ``Smartsheet-Hook-Response`` header. If we
   miss the challenge, Smartsheet marks the webhook ``DISABLED_VERIFICATION_FAILED``.

2. **HMAC SHA-256 signature.** Subsequent event POSTs include the
   ``Smartsheet-Hmac-SHA256`` header with the hex digest of HMAC(body, secret).
   Smartsheet has a documented bug where the digest occasionally arrives as
   63 chars (leading zero stripped) — we left-pad with ``0`` before the
   constant-time compare.

Reference: https://smartsheet.redoc.ly/tag/webhooks
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import List, Mapping, Optional

from smarthub_core.platforms.shared.webhook_base import (
    BaseWebhookHandler,
    ChallengeResponse,
    NormalizedEvent,
    WebhookSignatureError,
)

log = logging.getLogger(__name__)


CHALLENGE_HEADER = "Smartsheet-Hook-Challenge"
CHALLENGE_HEADER_ALT = "X-Smartsheet-Hook-Challenge"  # observed in some clients
CHALLENGE_RESPONSE_HEADER = "Smartsheet-Hook-Response"
SIGNATURE_HEADER = "Smartsheet-Hmac-SHA256"

_HEX_DIGEST_LENGTH = 64


class SmartsheetWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Smartsheet.

    Stateless — safe to instantiate once per gateway process and reuse across
    requests.
    """

    @property
    def provider(self) -> str:
        return "smartsheet"

    # ----- challenge ------------------------------------------------------

    def handle_challenge(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Optional[ChallengeResponse]:
        challenge = _get_header(headers, CHALLENGE_HEADER) or _get_header(
            headers, CHALLENGE_HEADER_ALT
        )
        if challenge is None:
            return None
        return ChallengeResponse(
            body=challenge.encode("utf-8"),
            content_type="text/plain",
            status_code=200,
            headers={CHALLENGE_RESPONSE_HEADER: challenge},
        )

    # ----- signature verification -----------------------------------------

    def verify_signature(
        self,
        headers: Mapping[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        if not secret:
            raise WebhookSignatureError("no shared secret configured")

        signature = _get_header(headers, SIGNATURE_HEADER)
        if not signature:
            raise WebhookSignatureError(
                f"missing {SIGNATURE_HEADER} header"
            )

        # Smartsheet bug: sometimes sends a 63-char hex digest with the
        # leading zero stripped. Pad so the constant-time compare works.
        if len(signature) == _HEX_DIGEST_LENGTH - 1 and signature[0] != "0":
            signature = "0" + signature

        if len(signature) != _HEX_DIGEST_LENGTH:
            raise WebhookSignatureError(
                f"signature length is {len(signature)}, expected {_HEX_DIGEST_LENGTH}"
            )

        expected = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature.lower()):
            raise WebhookSignatureError("signature mismatch")
        return True

    # ----- event extraction -----------------------------------------------

    def extract_events(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> List[NormalizedEvent]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise WebhookSignatureError(f"malformed JSON body: {e}") from e

        raw_events = payload.get("events") or []
        if not isinstance(raw_events, list):
            log.warning(
                "smartsheet_webhook: events field is not a list — got %s",
                type(raw_events).__name__,
            )
            return []

        normalized: List[NormalizedEvent] = []
        for ev in raw_events:
            if not isinstance(ev, dict):
                continue
            object_type = (ev.get("objectType") or "unknown").lower()
            event_type_field = (ev.get("eventType") or "unknown").lower()
            normalized.append(
                NormalizedEvent(
                    event_type=f"smartsheet.{object_type}.{event_type_field}",
                    external_id=str(ev.get("id")) if ev.get("id") is not None else None,
                    raw=ev,
                    occurred_at=ev.get("timestamp"),
                )
            )
        return normalized


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup. Mappings may or may not normalise case."""
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None
