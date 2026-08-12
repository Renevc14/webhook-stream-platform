"""Webhook ingress routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.requests import ClientDisconnect

from smarthub_core.platforms.shared.webhook_base import WebhookSignatureError

from gateway.envelope import build_callback_envelope
from gateway.routing.registry import HandlerRegistry
from gateway.secrets.store import WebhookSecretStore
from gateway.streams.publisher import (
    StreamPublisher,
    StreamPublishError,
    UnknownToolError,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhooks"])

_registry: HandlerRegistry = HandlerRegistry.default()
_secret_store: Optional[WebhookSecretStore] = None
_publisher: Optional[StreamPublisher] = None


def configure_webhook_routes(
    *,
    registry: Optional[HandlerRegistry] = None,
    secret_store: Optional[WebhookSecretStore] = None,
    publisher: Optional[StreamPublisher] = None,
) -> None:
    global _registry, _secret_store, _publisher
    if registry is not None:
        _registry = registry
    if secret_store is not None:
        _secret_store = secret_store
    if publisher is not None:
        _publisher = publisher


@router.post("/{provider}/{tool_slug}/{org_id}")
async def receive_webhook(
    provider: str,
    tool_slug: str,
    org_id: str,
    request: Request,
) -> Response:
    """Dispatch incoming webhook to the provider handler."""
    try:
        handler = _registry.get(provider)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider: {provider}",
        )

    headers = dict(request.headers)

    # Challenge first — do not read body (Smartsheet ~5s SLA).
    challenge = handler.handle_challenge(headers, b"")
    if challenge is not None:
        log.info(
            "webhook_gateway: challenge provider=%s tool=%s org=%s",
            provider,
            tool_slug,
            org_id,
        )
        return Response(
            content=challenge.body,
            status_code=challenge.status_code,
            media_type=challenge.content_type,
            headers=dict(challenge.headers),
        )

    try:
        body = await request.body()
    except ClientDisconnect:
        log.info(
            "webhook_gateway: client disconnected provider=%s org=%s",
            provider,
            org_id,
        )
        return Response(status_code=status.HTTP_200_OK)

    if not body:
        log.info(
            "webhook_gateway: empty body provider=%s tool=%s org=%s",
            provider,
            tool_slug,
            org_id,
        )
        return Response(status_code=status.HTTP_200_OK)

    if _secret_store is None:
        log.error("webhook_gateway: secret store not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="secret store unavailable",
        )

    # Canonical provider id, not the raw path segment: the registry accepts any
    # casing, and the cache key must not fork on it (SH-215).
    secrets = await _secret_store.get_secrets(
        org_id, tool_slug, provider=handler.provider
    )
    if not secrets:
        log.warning(
            "webhook_gateway: no secrets for org=%s provider=%s tool=%s",
            org_id,
            handler.provider,
            tool_slug,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no webhook secrets configured",
        )

    verified_webhook_id: Optional[str] = None
    for webhook_id, secret in secrets.items():
        try:
            if handler.verify_signature(headers, body, secret):
                verified_webhook_id = webhook_id
                break
        except WebhookSignatureError:
            continue

    if verified_webhook_id is None:
        log.warning(
            "webhook_gateway: signature verification failed provider=%s org=%s",
            provider,
            org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )

    events = handler.extract_events(headers, body)
    if not events:
        # Verified but nothing actionable (e.g. only discussion events) —
        # ack without touching the stream.
        log.info(
            "webhook_gateway: no events provider=%s tool=%s org=%s",
            provider,
            tool_slug,
            org_id,
        )
        return Response(
            content=b'{"status":"accepted","events":0}',
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )

    # SH-153: one envelope per callback — raw is the full original body so
    # the consumer's row grouping (SH-110) sees the whole event batch.
    envelope = build_callback_envelope(
        provider=provider,
        tool_slug=tool_slug,
        org_id=org_id,
        body=body,
        events=events,
        webhook_id=verified_webhook_id,
    )

    if _publisher is None:
        log.error("webhook_gateway: stream publisher not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="stream publisher unavailable",
        )

    try:
        msg_id = await _publisher.publish(tool_slug, envelope)
    except UnknownToolError:
        log.warning(
            "webhook_gateway: unknown tool provider=%s tool=%s org=%s",
            provider,
            tool_slug,
            org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown tool",
        )
    except StreamPublishError as e:
        # 5xx → Smartsheet re-delivers; consumer dedup absorbs duplicates.
        log.error(
            "webhook_gateway: stream publish failed provider=%s tool=%s "
            "org=%s: %s",
            provider,
            tool_slug,
            org_id,
            e,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="event enqueue failed",
        )

    log.info(
        "webhook_gateway: accepted provider=%s tool=%s org=%s events=%d msg=%s",
        provider,
        tool_slug,
        org_id,
        len(events),
        msg_id,
    )

    return Response(
        content=b'{"status":"accepted","events":%d}' % len(events),
        status_code=status.HTTP_200_OK,
        media_type="application/json",
    )
