"""
Supabase JWT gate + security headers (Attachment Counter / BAU parity).

When SUPABASE_JWT_SECRET is set, require valid Bearer on non-exempt routes.
Production without secret → 503 fail-closed.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from src.api.jwt_auth import verify_supabase_jwt
from src.config.settings import Settings, get_settings

log = logging.getLogger(__name__)


def _i2c_production(settings: Settings) -> bool:
    return (settings.environment or "").strip().lower() == "production"


def _path_exempt_from_jwt(path: str) -> bool:
    if path in ("/", "/health", "/ready", "/docs", "/openapi.json", "/redoc"):
        return True
    if path.startswith("/webhook"):
        return True
    if path.startswith("/dlq"):
        return True
    if path == "/metrics":
        return True
    return False


def _cors_headers_for_browser(request: Request, allowed_origins: List[str]) -> dict:
    origin = (request.headers.get("origin") or "").strip()
    if not origin or origin not in allowed_origins:
        return {}
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "vary": "Origin",
    }


def register_i2c_hub_middlewares(app: FastAPI, allowed_origins: List[str]) -> None:
    """
    Register (1) security headers (2) Supabase JWT gate.
    Call after other add_middleware calls so this runs outermost on the request
    (JWT runs before inner stack; see FastAPI middleware order).
    """

    @app.middleware("http")
    async def i2c_security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    @app.middleware("http")
    async def i2c_supabase_jwt_gate(request: Request, call_next: Callable):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path or ""
        if _path_exempt_from_jwt(path):
            return await call_next(request)

        settings = get_settings()
        secret = (settings.supabase_jwt_secret or "").strip()

        if _i2c_production(settings) and not secret:
            if settings.i2c_jwt_optional_in_production:
                log.warning(
                    "I2C: production without SUPABASE_JWT_SECRET — legacy mode "
                    "(I2C_JWT_OPTIONAL_IN_PRODUCTION=1). Remove after JWT is in Secrets Manager."
                )
                request.state.i2c_jwt_mode = "legacy"
                return await call_next(request)
            log.error("I2C: SUPABASE_JWT_SECRET missing in production — failing closed")
            return JSONResponse(
                status_code=503,
                content={"detail": "Service misconfigured (SUPABASE_JWT_SECRET missing)"},
                headers=_cors_headers_for_browser(request, allowed_origins),
            )

        if not secret:
            request.state.i2c_jwt_mode = "legacy"
            return await call_next(request)

        auth = (
            request.headers.get("Authorization") or request.headers.get("authorization") or ""
        ).strip()
        payload = verify_supabase_jwt(auth, secret)
        if not payload:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing session token"},
                headers=_cors_headers_for_browser(request, allowed_origins),
            )

        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token: missing sub"},
                headers=_cors_headers_for_browser(request, allowed_origins),
            )

        header_uid = (
            request.headers.get("X-User-ID") or request.headers.get("X-User-Id") or ""
        ).strip()
        if header_uid and header_uid != sub:
            return JSONResponse(
                status_code=403,
                content={"detail": "X-User-ID does not match session"},
                headers=_cors_headers_for_browser(request, allowed_origins),
            )

        request.state.i2c_jwt_mode = "jwt"
        request.state.i2c_jwt_sub = sub
        return await call_next(request)
