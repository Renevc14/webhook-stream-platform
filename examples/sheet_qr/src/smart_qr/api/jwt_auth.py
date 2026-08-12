"""Supabase JWT helpers (I2C-compatible)."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWKClient

log = logging.getLogger(__name__)
_jwks_client: Optional[PyJWKClient] = None
_jwks_lock = threading.Lock()


def _get_jwks_client() -> Optional[PyJWKClient]:
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    with _jwks_lock:
        if _jwks_client is not None:
            return _jwks_client
        supabase_url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
        if not supabase_url:
            return None
        jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
        try:
            _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        except Exception as exc:  # noqa: BLE001
            log.warning("JWKS client init failed: %s", exc)
            return None
        return _jwks_client


def verify_supabase_jwt(authorization_header: Optional[str], secret: str) -> Optional[Dict[str, Any]]:
    if not authorization_header:
        return None
    raw = authorization_header.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return None
    try:
        unverified_header = jwt.get_unverified_header(raw)
    except jwt.PyJWTError:
        return None
    alg = unverified_header.get("alg", "HS256")
    if alg == "ES256":
        client = _get_jwks_client()
        if client is None:
            return None
        try:
            key = client.get_signing_key_from_jwt(raw).key
            return jwt.decode(
                raw,
                key,
                algorithms=["ES256"],
                audience="authenticated",
                options={"verify_aud": False},
            )
        except jwt.PyJWTError:
            return None
    if not secret:
        return None
    try:
        return jwt.decode(
            raw,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        return None
