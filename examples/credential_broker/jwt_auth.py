"""
Supabase Auth JWT verification (HS256 + ES256).

Supports both legacy HS256 (symmetric) and newer ES256 (asymmetric) Supabase
projects.  For ES256 the public key is fetched from the project's JWKS endpoint
and cached for the lifetime of the process.

SUPABASE_JWT_SECRET is used for HS256.
SUPABASE_URL is used to derive the JWKS endpoint for ES256.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

import jwt
from jwt import PyJWKClient


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
        except Exception as e:
            print(f"[JWT] WARNING: could not create JWKS client from {jwks_url}: {e}")
            return None
        return _jwks_client


def verify_supabase_jwt(authorization_header: Optional[str], secret: str) -> Optional[Dict[str, Any]]:
    """Validate Authorization: Bearer <access_token> with HS256 or ES256."""
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
        return _verify_es256(raw, unverified_header)
    else:
        return _verify_hs256(raw, secret)


def _verify_hs256(raw: str, secret: str) -> Optional[Dict[str, Any]]:
    if not secret:
        return None
    try:
        payload = jwt.decode(
            raw,
            secret,
            algorithms=["HS256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": False,
            },
        )
        return payload if isinstance(payload, dict) else None
    except jwt.PyJWTError:
        return None


def _verify_es256(raw: str, header: dict) -> Optional[Dict[str, Any]]:
    client = _get_jwks_client()
    if not client:
        print("[JWT] ES256 token received but JWKS client unavailable (no SUPABASE_URL?)")
        return None
    try:
        signing_key = client.get_signing_key_from_jwt(raw)
        payload = jwt.decode(
            raw,
            signing_key.key,
            algorithms=["ES256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": False,
            },
        )
        return payload if isinstance(payload, dict) else None
    except jwt.PyJWTError as e:
        print(f"[JWT] ES256 verification failed: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"[JWT] ES256 unexpected error: {type(e).__name__}: {e}")
        return None
