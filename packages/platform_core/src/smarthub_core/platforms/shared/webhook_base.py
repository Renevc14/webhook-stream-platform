"""Provider-agnostic webhook handler base.

The polyglot Webhook Gateway dispatches incoming webhooks via path segment:
``POST /webhook/{provider}/{tool_slug}/{org_id}``. The handler registry maps
``{provider}`` to a ``BaseWebhookHandler`` impl living under
``platforms/{provider}/webhook.py``.

A handler's job is narrow:

1. **Verify the signature** of the incoming request against the provider's
   shared secret. Each provider has a different scheme (HMAC-SHA256 of body,
   HMAC-SHA256 of timestamp+body, HMAC-SHA1 of URL+sorted-params, ...).
2. **Handle the challenge** if the provider uses one. Smartsheet sends a sync
   challenge in the body; Slack sends a ``url_verification`` event with a
   ``challenge`` field; HubSpot/Stripe/Twilio have no challenge.
3. **Extract events** from the payload into a ``NormalizedEvent`` so the
   gateway can build the stream envelope. Providers send batched events with
   different shapes — normalization happens here, not in the workers.

The handler returns; the gateway is responsible for cache lookup of secrets,
HMAC validation orchestration, and writing to the stream. Handlers don't
touch Redis or Postgres directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional


class WebhookSignatureError(Exception):
    """Raised when a webhook fails signature verification.

    The gateway must respond 401 (or 403 — provider-specific) and not enqueue
    the event. Callers should treat this as suspect; log with the request ID
    but do not log the body."""


@dataclass(frozen=True)
class ChallengeResponse:
    """A handler's response to a provider's verification challenge.

    Fields:
      body:         Raw response body the gateway returns. Usually bytes; some
                    providers expect the original challenge value echoed back
                    (Smartsheet) or a JSON object (Slack ``{"challenge": ...}``).
      content_type: MIME type for the response.
      status_code:  HTTP status — almost always 200; Slack's URL verification
                    is also 200 with a JSON body.
      headers:      Optional extra headers (``Smartsheet-Hook-Response`` for
                    Smartsheet's challenge ack, etc.).
    """

    body: bytes
    content_type: str = "text/plain"
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedEvent:
    """One event extracted from a webhook payload, in a uniform shape.

    Providers send batched payloads with provider-specific structure.
    ``BaseWebhookHandler.extract_events`` returns one of these per logical
    event; the gateway turns each one into a stream envelope.

    Fields:
      event_type:    Provider-namespaced event name — ``smartsheet.row.added``,
                     ``hubspot.contact.created``, ``slack.message``, etc. Used
                     downstream by workers to filter.
      external_id:   Provider's event identifier when present, used for dedup
                     in addition to our generated ``event_id``.
      raw:           The provider's original event sub-object, untransformed.
                     The worker is responsible for parsing this.
      occurred_at:   ISO-8601 timestamp from the provider, when present.
      tool_hint:     Optional tool slug if the handler can determine it from
                     the payload (Smartsheet webhooks are scoped per-sheet so
                     the URL path tells us; HubSpot webhooks may have multiple
                     subscriptions for the same portal).
    """

    event_type: str
    external_id: Optional[str] = None
    raw: Any = None
    occurred_at: Optional[str] = None
    tool_hint: Optional[str] = None


class BaseWebhookHandler(ABC):
    """Provider-agnostic webhook handler interface.

    One impl per provider, registered in the Webhook Gateway's handler
    registry. The gateway calls these methods in this order:

    1. ``handle_challenge(headers, body)`` — if it returns a ``ChallengeResponse``,
       the gateway returns that immediately and does NOT proceed to step 2-3.
    2. ``verify_signature(headers, body, secret)`` — must return ``True`` or
       raise ``WebhookSignatureError``.
    3. ``extract_events(headers, body)`` — returns the events to enqueue.

    Implementations are stateless and safe to share across requests.
    """

    @property
    @abstractmethod
    def provider(self) -> str:
        """Lowercase provider identifier matching the URL path segment."""

    @abstractmethod
    def handle_challenge(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Optional[ChallengeResponse]:
        """If the request is a verification challenge, return the response;
        otherwise return ``None`` and let the gateway continue to signature
        verification.

        Smartsheet: detect ``Smartsheet-Hook-Challenge`` header.
        Slack:      detect ``"type": "url_verification"`` in body JSON.
        HubSpot/Stripe/Twilio: always return ``None``.
        """

    @abstractmethod
    def verify_signature(
        self,
        headers: Mapping[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify the request's signature against ``secret``.

        Must use a constant-time comparison (``hmac.compare_digest``). Must
        raise ``WebhookSignatureError`` with a short message on mismatch (so
        the gateway can return 401 with a stable error). Must also raise on
        missing/malformed signature headers.
        """

    @abstractmethod
    def extract_events(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> List[NormalizedEvent]:
        """Parse the payload into one or more ``NormalizedEvent``.

        Returns an empty list if the payload contains no actionable events
        (e.g., a Smartsheet webhook that arrived with only ``discussion``
        events when the tool only cares about attachments)."""
