from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from smart_qr.adapters.db.pool import apply_migrations, close_pool, get_pool, init_pool
from smart_qr.adapters.platform.memberships import (
    close_membership_reader,
    get_membership_reader,
)
from smart_qr.adapters.smartsheet.credentials import (
    close_credential_resolver,
    get_credential_resolver,
)
from smart_qr.api.browse import router as browse_router
from smart_qr.api.request_context import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    request_id_var,
)
from smart_qr.api.v1.routes import router as v1_router
from smart_qr.config.settings import get_settings
from smart_qr.workers.stream_consumer import start_stream_consumer, stop_stream_consumer

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] req=%(request_id)s %(message)s"


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.strip().upper(), logging.INFO),
        format=LOG_FORMAT,
    )
    request_filter = RequestIdFilter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(request_filter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    await init_pool(
        settings.database_url,
        ssl_mode=settings.database_ssl,
        statement_cache_size=settings.database_statement_cache_size,
    )
    await apply_migrations(settings.migrations_dir)
    # Logs whether org membership resolves through the platform or direct SQL, so a
    # missing service role key is visible at boot instead of as 403s under load.
    get_membership_reader()
    # Same reason: say at boot whether Smartsheet credentials come from the OAuth
    # broker or from a local token, instead of discovering it on the first write.
    get_credential_resolver(settings)
    # Instantiate billing early so mock-outside-local fails at boot, and so the
    # shared core Redis pool is the one used for bolt-cap (not a private client).
    from smart_qr.adapters.billing.port import get_billing, reset_billing

    await get_billing(settings)
    consumer_task = None
    if settings.run_stream_consumer:
        consumer_task = await start_stream_consumer(settings)
    log.info("SmartQR started env=%s port=%s", settings.app_env, settings.port)
    yield
    await stop_stream_consumer(consumer_task)
    await reset_billing()
    await close_credential_resolver()
    await close_membership_reader()
    await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    allowed_origins = settings.cors_allowed_origins
    log.info("SmartQR CORS allow_origins (%d): %s", len(allowed_origins), allowed_origins)

    app = FastAPI(title="SmartQR", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-User-ID",
            "X-Organization-ID",
            "X-Request-Id",
            "Idempotency-Key",
        ],
    )
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER) or request.headers.get(
            REQUEST_ID_HEADER.lower()
        )
        request_id = (incoming or uuid.uuid4().hex[:12])[:64]
        token = request_id_var.set(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
            if request.url.path not in ("/health", "/ready"):
                log.info(
                    "%s %s -> %s in %sms",
                    request.method,
                    request.url.path,
                    response.status_code,
                    int((time.monotonic() - started) * 1000),
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        except Exception:
            log.exception(
                "%s %s failed after %sms",
                request.method,
                request.url.path,
                int((time.monotonic() - started) * 1000),
            )
            raise
        finally:
            request_id_var.reset(token)

    # No webhook route: Smartsheet callbacks land on the shared gateway, which
    # verifies the HMAC and publishes to stream:smart-qr. SmartQR only consumes
    # (see workers/stream_worker.py).
    app.include_router(v1_router)
    app.include_router(browse_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "smart-qr"}

    @app.get("/ready")
    async def ready(response: Response) -> dict:
        """Postgres is required, so a DB failure must fail the probe (503)."""
        settings = get_settings()
        db_ok = False
        try:
            pool = get_pool()
            db_ok = (await pool.fetchval("SELECT 1")) == 1
        except Exception as exc:  # noqa: BLE001
            log.error("Postgres ready check failed: %s", exc)

        redis_ok = False
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url)
            redis_ok = (await client.ping()) is True
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis ready check failed: %s", exc)

        if db_ok and redis_ok:
            status_text = "ok"
        elif db_ok:
            status_text = "degraded"
        else:
            status_text = "not_ready"
            response.status_code = 503
        return {"status": status_text, "database": db_ok, "redis": redis_ok}

    return app


app = create_app()
