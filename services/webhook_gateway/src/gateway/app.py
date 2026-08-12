"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI

from gateway.config.settings import get_settings
from gateway.routes import health, internal, webhooks
from gateway.routing.registry import HandlerRegistry
from gateway.secrets.store import (
    InMemoryWebhookSecretStore,
    RedisWebhookSecretStore,
    WebhookSecretStore,
)
from gateway.streams.publisher import StreamPublisher

log = logging.getLogger(__name__)


def create_app(
    *,
    registry: Optional[HandlerRegistry] = None,
    secret_store: Optional[WebhookSecretStore] = None,
    publisher: Optional[StreamPublisher] = None,
) -> FastAPI:
    settings = get_settings()

    injected = secret_store is not None or publisher is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        store = secret_store
        stream_publisher = publisher
        redis_client = None

        if not injected:
            if settings.redis_url:
                try:
                    from smarthub_core.db.redis import RedisClient

                    redis_client = RedisClient(settings.redis_url)
                    await redis_client.start()
                    store = RedisWebhookSecretStore(redis_client)
                    stream_publisher = StreamPublisher(
                        redis_client, registry=settings.tool_registry()
                    )
                    health.set_redis_ping(redis_client.health_check)
                    log.info(
                        "webhook_gateway: Redis wired (secret store + stream publisher)"
                    )
                except Exception as exc:
                    log.error(
                        "webhook_gateway: Redis unavailable — starting DEGRADED "
                        "(no stream publish, event POSTs will 503): %s",
                        exc,
                    )
                    store = InMemoryWebhookSecretStore()
                    redis_client = None
            else:
                store = InMemoryWebhookSecretStore()
                log.warning(
                    "webhook_gateway: no REDIS_URL — in-memory secret store, "
                    "stream publish disabled"
                )

            webhooks.configure_webhook_routes(
                registry=registry or HandlerRegistry.default(),
                secret_store=store,
                publisher=stream_publisher,
            )
            if store is not None:
                internal.configure_internal_routes(secret_store=store)

            if redis_client is not None and store is not None:
                from gateway.lifecycle.manager import start_lifecycle

                await start_lifecycle(
                    redis_client=redis_client,
                    secret_store=store,
                    settings=settings,
                )

        yield

        from gateway.lifecycle.manager import stop_lifecycle

        await stop_lifecycle()

        if redis_client is not None:
            try:
                await redis_client.close()
            except Exception as exc:
                log.warning("webhook_gateway: Redis close failed: %s", exc)

    app = FastAPI(
        title="Smarthub Webhook Gateway",
        version="0.3.0",
        lifespan=lifespan,
    )

    if injected:
        webhooks.configure_webhook_routes(
            registry=registry or HandlerRegistry.default(),
            secret_store=secret_store,
            publisher=publisher,
        )
        if secret_store is not None:
            internal.configure_internal_routes(secret_store=secret_store)

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(internal.router)
    return app
