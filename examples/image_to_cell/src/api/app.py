"""
FastAPI Application Factory
===========================

Creates and configures the FastAPI application.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.cors_config import build_i2c_allowed_origins
from src.api.routes import health, webhooks, configs, dlq, smartsheet, manual_trigger_route
from src.api.middleware.rate_limiter import RateLimiterMiddleware
from src.api.middleware.auth import AuthMiddleware, RequestLoggingMiddleware
from src.api.middleware.supabase_jwt_gate import register_i2c_hub_middlewares
from src.api.middleware.graceful_shutdown import (
    GracefulShutdownMiddleware,
    get_request_tracker,
)
from src.config.settings import get_settings
from src.workers.poller import get_poller_worker
from src.workers.webhook_lifecycle import (
    get_webhook_lifecycle_manager,
    start_lifecycle_manager,
    stop_lifecycle_manager,
)
from src.workers.dlq_processor import (
    get_dlq_processor,
    start_dlq_processor,
    stop_dlq_processor,
)
from src.workers.stream_consumer import start_stream_consumer, stop_stream_consumer
from src.services.redis_client import set_redis_client
from src.services.webhook_dedup import WebhookDedupService, set_dedup_service
from src.utils.logging import setup_logging, log_separator, FlowTags

from smarthub_core.db.redis import RedisClient


# Initialize logging at module load
settings = get_settings()
setup_logging(
    level=settings.log_level,
    json_format=settings.log_json_format,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    settings = get_settings()

    log_separator(log, "IMAGE-TO-CELL v3 STARTING")
    log.info(f"{FlowTags.STARTUP} Environment: {settings.environment}")

    # TODO: Re-enable after fixing field name mapping
    # Validate configurations
    # log.info(f"{FlowTags.STARTUP} Validating configurations...")
    # validation_result = validate_configs_on_startup()
    # if validation_result.has_errors:
    #     log.error(
    #         f"{FlowTags.STARTUP} Configuration errors found - "
    #         "some features may not work correctly"
    #     )

    background_tasks = []

    log.info(
        f"{FlowTags.STARTUP} Runtime roles: {settings.runtime_roles_summary}"
        + (f" (APP_MODE={settings.app_mode})" if settings.app_mode else "")
    )

    # SH-99: Initialise the cross-process webhook dedup service.
    # Best-effort: if Redis is unreachable we log a WARN and install an
    # in-memory-only service so the app still starts (degraded — dedup
    # protects within a single task only).
    redis_client: Optional[RedisClient] = None
    if settings.redis_url:
        redis_client = RedisClient(settings.redis_url)
        try:
            await redis_client.start()
            log.info(
                f"{FlowTags.STARTUP} SH-99 Redis dedup wired url=%s",
                redis_client._sanitize_url(),
            )
        except Exception as e:
            log.warning(
                f"{FlowTags.STARTUP} SH-99 Redis unavailable at startup (%s) "
                "— falling back to in-memory webhook dedup (per-task only)",
                e,
            )
            redis_client = None
    else:
        log.warning(
            f"{FlowTags.STARTUP} SH-99 REDIS_URL not configured — falling "
            "back to in-memory webhook dedup (per-task only). Set REDIS_URL "
            "to enable cross-task dedup."
        )

    set_dedup_service(
        WebhookDedupService(
            client=redis_client,
            ttl_s=settings.redis_dedup_ttl_s,
        )
    )
    # SH-93: same client is needed by SmartsheetClient (token cache + rate
    # limit). Install once on a shared accessor so any factory can fetch it.
    set_redis_client(redis_client)

    # Shared bolt metering (smarthub_core.billing) — same Redis pool as dedup.
    # Built after Redis is installed so BoltGate sees the wired client (or
    # None outside local → fail-closed reserves).
    from src.adapters.billing.port import get_billing, reset_billing

    try:
        await get_billing(settings)
    except Exception as e:
        log.error(
            f"{FlowTags.STARTUP} Billing init failed (%s) — reserves will fail closed",
            e,
        )

    # Start webhook lifecycle manager if enabled (SH-151: run_lifecycle_poller)
    if settings.webhook_lifecycle_enabled and settings.run_lifecycle_poller:
        log.info(f"{FlowTags.STARTUP} Starting webhook lifecycle manager...")
        log.info(
            f"{FlowTags.STARTUP} Health check interval: "
            f"{settings.webhook_lifecycle_interval_s}s"
        )
        if settings.has_webhook_callback_config:
            if settings.smartsheet_webhook_callback_base_url:
                log.info(
                    f"{FlowTags.STARTUP} Gateway callback base configured "
                    f"(per-org URLs via SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL)"
                )
            else:
                log.info(f"{FlowTags.STARTUP} Callback URL configured (legacy)")
        else:
            log.warning(
                f"{FlowTags.STARTUP} No webhook callback URL configured - "
                "auto-registration disabled "
                "(set WEBHOOK_CALLBACK_URL or SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL)"
            )

        lifecycle_task = asyncio.create_task(start_lifecycle_manager())
        background_tasks.append(("webhook_lifecycle", lifecycle_task))
    elif settings.webhook_lifecycle_enabled and not settings.run_lifecycle_poller:
        log.info(
            f"{FlowTags.STARTUP} RUN_LIFECYCLE_POLLER=false — "
            "webhook lifecycle manager skipped"
        )

    # Start poller if enabled
    poller = None
    if settings.enable_polling:
        log.info(f"{FlowTags.STARTUP} Starting poller worker...")
        poller = get_poller_worker()
        poller_task = asyncio.create_task(poller.start())
        background_tasks.append(("poller", poller_task))

    # Start DLQ processor if enabled
    if settings.dlq_processor_enabled:
        log.info(f"{FlowTags.STARTUP} Starting DLQ processor...")
        log.info(
            f"{FlowTags.STARTUP} DLQ interval: {settings.dlq_processor_interval_s}s, "
            f"batch size: {settings.dlq_batch_size}"
        )
        dlq_task = asyncio.create_task(start_dlq_processor())
        background_tasks.append(("dlq_processor", dlq_task))

    if settings.run_stream_consumer:
        consumer_task = await start_stream_consumer()
        background_tasks.append(("stream_consumer", consumer_task))
    else:
        log.info(f"{FlowTags.STARTUP} RUN_STREAM_CONSUMER=false — stream consumer skipped")

    log_separator(log, "STARTUP COMPLETE")

    yield

    # Shutdown
    log_separator(log, "SHUTTING DOWN")

    # Wait for active requests to complete
    tracker = get_request_tracker()
    if tracker.active_count > 0:
        log.info(
            f"{FlowTags.SHUTDOWN} Waiting for {tracker.active_count} "
            "active requests to complete..."
        )
        await tracker.wait_for_shutdown(timeout=30.0)

    for name, task in background_tasks:
        log.info(f"{FlowTags.SHUTDOWN} Stopping {name}...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if settings.webhook_lifecycle_enabled and settings.run_lifecycle_poller:
        await stop_lifecycle_manager()

    if settings.dlq_processor_enabled:
        await stop_dlq_processor()

    if settings.run_stream_consumer:
        await stop_stream_consumer()

    if poller:
        await poller.stop()

    try:
        await reset_billing()
    except Exception as e:
        log.warning(f"{FlowTags.SHUTDOWN} Billing reset failed: {e}")

    # SH-99: tear down the Redis connection pool.
    if redis_client is not None:
        try:
            await redis_client.close()
        except Exception as e:
            log.warning(f"{FlowTags.SHUTDOWN} SH-99 Redis close failed: {e}")

    log.info(f"{FlowTags.SHUTDOWN} Shutdown complete")


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application
    """
    settings = get_settings()
    allowed_origins = build_i2c_allowed_origins(settings)
    log.info(
        "I2C CORS allow_origins (%d): %s",
        len(allowed_origins),
        allowed_origins,
    )

    openapi_enabled = (
        settings.environment.strip().lower() != "production"
        and not settings.i2c_disable_openapi
    )

    app = FastAPI(
        title="Image-to-Cell API",
        description="Smartsheet image processing service",
        version="3.0.0",
        lifespan=lifespan,
        docs_url="/docs" if openapi_enabled else None,
        redoc_url="/redoc" if openapi_enabled else None,
        openapi_url="/openapi.json" if openapi_enabled else None,
    )

    _i2c_cors_allow_headers = [
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "X-User-ID",
        "X-User-Id",
        "X-Organization-ID",
        "X-Admin-Key",
        "X-API-Key",
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=_i2c_cors_allow_headers,
    )

    app.add_middleware(RequestLoggingMiddleware, log_body=False)
    app.add_middleware(GracefulShutdownMiddleware, reject_during_shutdown=True)

    if settings.enable_rate_limiting:
        app.add_middleware(
            RateLimiterMiddleware,
            requests_per_minute=settings.api_rate_limit,
            burst_size=settings.api_burst_size,
            webhook_requests_per_minute=settings.webhook_rate_limit,
            webhook_burst_size=settings.webhook_burst_size,
            exclude_paths=["/health", "/ready", "/"],
        )

    if settings.api_key:
        app.add_middleware(AuthMiddleware)

    register_i2c_hub_middlewares(app, allowed_origins)

    app.include_router(health.router)

    if settings.run_http_api:
        if settings.run_webhook_receiver:
            app.include_router(webhooks.router)
        else:
            log.info(
                "RUN_WEBHOOK_RECEIVER=false — direct /webhook/smartsheet omitted "
                "(gateway owns Smartsheet ingress)"
            )
        app.include_router(configs.router)
        app.include_router(dlq.router)
        app.include_router(smartsheet.router)
        app.include_router(manual_trigger_route.router)
    else:
        log.info("RUN_HTTP_API=false — API routes omitted (health only)")

    @app.get("/")
    async def alb_compat_root():
        """Many ALBs default health check path to `/` — return 200 so targets register."""
        return {"service": "image-to-cell", "status": "ok"}

    return app


# Default app instance
app = create_app()
