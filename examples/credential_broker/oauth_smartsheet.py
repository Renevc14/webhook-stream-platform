"""
Smartsheet OAuth for the credentials service (encrypt-tool).

Implements the 3-legged OAuth flow, per-tool token minting, the internal
token broker used by tool backends, and single-flight refresh with rotation.

Endpoints (registered as a Flask blueprint by app.py):

  POST /oauth/start        user JWT   → authorize URL + state for one tool
  POST /oauth/exchange     user JWT   → exchange code, encrypt + persist tokens
  POST /oauth/disconnect   user JWT   → revoke at Smartsheet + mark rows revoked
  GET  /oauth/status       user JWT   → connection + per-tool token health
  POST /internal/token/resolve  broker secret → decrypted access token + base_url
                                + explicit oauth_state contract (SH-182): every
                                response reports oauth_state / fallback_used /
                                fallback_reason so consumers can distinguish a
                                healthy runner from transition-PAT continuity;
                                revoked/terminal with no PAT is a typed 409.

Security invariants:
  - Client secrets and refresh tokens never leave this service.
  - Tokens are AES-256-GCM encrypted with AAD binding (org:user:slug:kind).
  - state is single-use, expires in 10 minutes, stored hashed, bound to
    user + org + tool.
  - The broker endpoint is authenticated with a shared secret from Secrets
    Manager (SMARTSHEET_BROKER_SECRET) using constant-time comparison and
    must never be exposed on a public route path by the load balancer.

Environment:
  SMARTSHEET_OAUTH_CLIENT_ID_US / SMARTSHEET_OAUTH_CLIENT_SECRET_US
  SMARTSHEET_OAUTH_CLIENT_ID_EU / SMARTSHEET_OAUTH_CLIENT_SECRET_EU
  SMARTSHEET_BROKER_SECRET           (service-to-service auth for /internal/token/resolve)
  SMARTSHEET_OAUTH_REFRESH_SWEEP_SECONDS (default 900)
  SMARTSHEET_OAUTH_REFRESH_WINDOW_HOURS  (default 24)

The redirect URI is fixed in the Smartsheet app registration (the hub's
/smartsheet-callback.html page); neither the authorize URL nor the token
exchange needs to carry it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Blueprint, g, jsonify, request

import audit_log

# ═══════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════

OAUTH_REGIONS = {
    "US": {"authorize_base": "https://app.smartsheet.com"},
    "EU": {"authorize_base": "https://app.smartsheet.eu"},
}

# Least-privilege scopes per tool. GET /users/me works with any valid token
# regardless of scope, so verification never needs extra scopes.
TOOL_SCOPES: Dict[str, List[str]] = {
    "bulk-attachment-uploader": ["READ_SHEETS", "WRITE_SHEETS"],
    "smart-image-to-cell": ["READ_SHEETS", "WRITE_SHEETS", "ADMIN_WEBHOOKS"],
    # Cell History (polling + webhook) creates/locks CH columns, writes cells,
    # and (v2) manages webhooks. ADMIN_SHEETS is required for column structure
    # changes per Smartsheet's OAuth scope map; WRITE_SHEETS alone is not enough.
    # READ_USERS is required so CH-2 can resolve webhook userId -> name/email
    # via GET /users/{id}; without it Changed By falls back to "User <id>".
    "single-cell-audit": [
        "READ_SHEETS", "WRITE_SHEETS", "ADMIN_SHEETS", "ADMIN_WEBHOOKS",
        "READ_USERS",
    ],
    # SmartQR writes generated codes into cell images and registers its own
    # sheet webhooks, so it needs the same shape as image-to-cell.
    "smart-qr": ["READ_SHEETS", "WRITE_SHEETS", "ADMIN_WEBHOOKS"],
    "count-my-attachments": ["READ_SHEETS", "WRITE_SHEETS"],
    "attachment-info": ["READ_SHEETS", "WRITE_SHEETS"],  # alias of count-my-attachments
    "dynamic-ddl": ["READ_SHEETS", "WRITE_SHEETS", "ADMIN_SHEETS", "ADMIN_WORKSPACES"],
    "column-id-checker": ["READ_SHEETS", "READ_SIGHTS"],
    "textnow": ["READ_SHEETS", "WRITE_SHEETS", "ADMIN_WEBHOOKS"],
    "bulk-sharing-permissions-changer": [
        "READ_SHEETS", "READ_SIGHTS", "SHARE_SHEETS", "SHARE_SIGHTS", "ADMIN_WORKSPACES",
    ],
}
DEFAULT_SCOPES = ["READ_SHEETS"]

# Slugs that are live (or about to be). Kept as its own set — not derived from
# TOOL_SCOPES — so deleting an entry from the table is caught at import time.
# A missing entry silently falls back to DEFAULT_SCOPES (READ_SHEETS only),
# which is how Cell History (SH-201) and SmartQR lost WRITE_SHEETS in staging
# and answered every cellimages call with 1004.
REQUIRED_TOOL_SLUGS = frozenset(
    {
        "bulk-attachment-uploader",
        "smart-image-to-cell",
        "single-cell-audit",
        "smart-qr",
        "count-my-attachments",
        "attachment-info",
        "dynamic-ddl",
        "column-id-checker",
        "textnow",
        "bulk-sharing-permissions-changer",
    }
)


def assert_tool_scopes_complete(
    required: Optional[frozenset[str]] = None,
    scopes: Optional[Dict[str, List[str]]] = None,
) -> None:
    """Fail loudly when a deployed tool has no entry in TOOL_SCOPES.

    Called at process start. Without this, a new tool ships, the broker grants
    DEFAULT_SCOPES, and the first write fails with a permission error that looks
    like a user-access problem.
    """
    table = scopes if scopes is not None else TOOL_SCOPES
    need = required if required is not None else REQUIRED_TOOL_SLUGS
    missing = sorted(need - frozenset(table.keys()))
    if missing:
        raise RuntimeError(
            "TOOL_SCOPES is missing entries for: "
            + ", ".join(missing)
            + f". Without an entry the OAuth broker grants DEFAULT_SCOPES "
            f"({DEFAULT_SCOPES}) and write tools fail with Smartsheet 1004."
        )


# Refuse to boot a credentials service that would under-scope a known tool.
assert_tool_scopes_complete()

STATE_TTL_MINUTES = 10
REFRESH_LEASE_SECONDS = 120        # stale refresh claims reclaimable after this
RESOLVE_REFRESH_THRESHOLD_SECONDS = 600  # refresh inline if expiring within 10 min
MAX_PENDING_STATES_PER_USER = 25

# Rate limits (OWASP API4). In-memory sliding windows — per-process, which is
# fine as a first line of defence; ALB/WAF rules remain the fleet-wide layer.
OAUTH_RATE_LIMIT_PER_MIN = 30          # per caller (user or IP) across /oauth/*
BROKER_AUTH_FAIL_LIMIT = 5             # bad broker secrets per IP before lockout
BROKER_AUTH_FAIL_WINDOW_SECONDS = 600  # lockout window

_IS_PROD = os.getenv("ENVIRONMENT", "").lower() == "production"

_rate_lock = threading.Lock()
_rate_windows: Dict[str, List[float]] = {}


def _rate_limited(key: str, limit: int, window_seconds: float = 60.0,
                  record: bool = True) -> bool:
    """True when `key` has exceeded `limit` events inside the sliding window.

    With record=True (default) the current attempt is counted when under the
    limit; record=False is a peek-only check (used so successful broker auths
    never count toward the failure lockout).
    """
    now = time.time()
    with _rate_lock:
        times = [t for t in _rate_windows.get(key, []) if now - t < window_seconds]
        if len(times) >= limit:
            _rate_windows[key] = times
            return True
        if record:
            times.append(now)
        _rate_windows[key] = times
        return False


def _client_ip() -> str:
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or (request.remote_addr or "unknown")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _client_creds(region: str) -> Tuple[Optional[str], Optional[str]]:
    r = (region or "").upper()
    return (
        os.getenv(f"SMARTSHEET_OAUTH_CLIENT_ID_{r}"),
        os.getenv(f"SMARTSHEET_OAUTH_CLIENT_SECRET_{r}"),
    )


def scopes_for_tool(product_slug: str) -> List[str]:
    scopes = TOOL_SCOPES.get(product_slug)
    if scopes is not None:
        return scopes
    # Unknown slug: keep DEFAULT so a brand-new tool still connects during
    # development, but shout — this is the path that under-scoped SmartQR.
    import logging

    logging.getLogger(__name__).error(
        "TOOL_SCOPES has no entry for product_slug=%s — granting DEFAULT_SCOPES "
        "%s. Add the slug to TOOL_SCOPES and REQUIRED_TOOL_SLUGS before deploy.",
        product_slug,
        DEFAULT_SCOPES,
    )
    return list(DEFAULT_SCOPES)


def missing_scopes_for_tool(
    product_slug: str, token_scopes: Optional[List[str]]
) -> List[str]:
    """Return required scopes the token does not currently hold (sorted).

    Used by ``/oauth/status`` so an under-scoped but otherwise ``active``
    token surfaces ``needs_reconnect`` — refresh cannot widen scopes; the
    user must re-consent.
    """
    required = set(scopes_for_tool(product_slug))
    have = set(token_scopes or [])
    return sorted(required - have)


def _tools_status_payload(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the per-tool objects for ``GET /oauth/status``."""
    out: List[Dict[str, Any]] = []
    for t in tokens:
        missing = missing_scopes_for_tool(t["product_slug"], t.get("scopes"))
        out.append(
            {
                "product_slug": t["product_slug"],
                "status": t["status"],
                "needs_reconnect": t["status"] != "active" or bool(missing),
                "missing_scopes": missing,
                "expires_at": t["expires_at"],
                "last_refreshed_at": t["last_refreshed_at"],
                "scopes": t.get("scopes") or [],
            }
        )
    return out


# ═══════════════════════════════════════════════════════════════════
#  Crypto: AES-256-GCM with AAD context binding
# ═══════════════════════════════════════════════════════════════════

def _aad(org_id: str, user_id: str, product_slug: str, kind: str) -> bytes:
    return f"ss-oauth:{org_id}:{user_id}:{product_slug}:{kind}".encode()


def encrypt_bound(key: bytes, plain: str, aad: bytes) -> str:
    iv = pysecrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, plain.encode(), aad)
    return base64.b64encode(iv + ct).decode()


def decrypt_bound(key: bytes, b64_blob: str, aad: bytes) -> str:
    blob = base64.b64decode(b64_blob)
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad).decode()


# ═══════════════════════════════════════════════════════════════════
#  Smartsheet token endpoint helpers
# ═══════════════════════════════════════════════════════════════════

def _token_request(base_url: str, params: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
    """POST to the regional /token endpoint. Returns (ok, payload-or-error)."""
    url = f"{base_url.rstrip('/')}/token"
    try:
        resp = requests.post(
            url,
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except requests.exceptions.RequestException as e:
        return False, {"error_code": "SMARTSHEET_UNREACHABLE", "detail": str(e)[:200]}

    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code != 200 or "access_token" not in body:
        return False, {
            "error_code": "OAUTH_EXCHANGE_FAILED",
            "http_status": resp.status_code,
            "smartsheet_error": str(body.get("error", ""))[:120],
            "smartsheet_error_code": body.get("errorCode"),
        }
    return True, body


def exchange_authorization_code(base_url: str, region: str, code: str) -> Tuple[bool, Dict[str, Any]]:
    client_id, client_secret = _client_creds(region)
    if not client_id or not client_secret:
        return False, {"error_code": "REGION_UNSUPPORTED", "detail": f"No OAuth app configured for region {region}"}
    # Smartsheet's alternative to sending the raw secret: hash = SHA-256(secret|code)
    code_hash = hashlib.sha256(f"{client_secret}|{code}".encode()).hexdigest()
    return _token_request(base_url, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "hash": code_hash,
    })


def refresh_token_pair(base_url: str, region: str, refresh_token: str) -> Tuple[bool, Dict[str, Any]]:
    client_id, client_secret = _client_creds(region)
    if not client_id or not client_secret:
        return False, {"error_code": "REGION_UNSUPPORTED", "detail": f"No OAuth app configured for region {region}"}
    token_hash = hashlib.sha256(f"{client_secret}|{refresh_token}".encode()).hexdigest()
    return _token_request(base_url, {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "hash": token_hash,
    })


def revoke_all_tokens(base_url: str, access_token: str) -> bool:
    """DELETE /token?deleteAllForApiClient=true revokes every token our app holds for this user."""
    try:
        resp = requests.delete(
            f"{base_url.rstrip('/')}/token",
            params={"deleteAllForApiClient": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except requests.exceptions.RequestException:
        return False


def revoke_single_token(base_url: str, access_token: str) -> bool:
    """DELETE /token (no params) revokes only the presented token pair — the
    per-tool revoke primitive; other tools' pairs stay live."""
    try:
        resp = requests.delete(
            f"{base_url.rstrip('/')}/token",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except requests.exceptions.RequestException:
        return False


def fetch_smartsheet_user(base_url: str, access_token: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=12,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.exceptions.RequestException:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
#  Audit
# ═══════════════════════════════════════════════════════════════════

def record_oauth_event(
    sb,
    *,
    request_id: str,
    event_type: str,
    outcome: str,
    http_status: Optional[int] = None,
    organization_id: Optional[str] = None,
    user_id: Optional[str] = None,
    product_slug: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    error_code: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    meta = dict(meta or {})
    for k in list(meta.keys()):
        if k.lower() in ("access_token", "refresh_token", "code", "authorization", "state"):
            meta.pop(k, None)

    line = {
        "audit": "smartsheet_oauth",
        "event_type": event_type,
        "outcome": outcome,
        "http_status": http_status,
        "organization_id": organization_id,
        "user_id": user_id,
        "product_slug": product_slug,
        "actor_user_id": actor_user_id,
        "request_id": request_id,
        "error_code": error_code,
        "meta": meta,
    }
    audit_log.emit_audit_line(line)

    if sb is None:
        return
    try:
        sb.table("smartsheet_oauth_audit_events").insert({
            "event_type": event_type,
            "outcome": outcome,
            "http_status": http_status,
            "organization_id": organization_id,
            "user_id": user_id,
            "product_slug": product_slug,
            "actor_user_id": actor_user_id,
            "request_id": request_id,
            "error_code": error_code,
            "meta": meta,
        }).execute()
    except Exception:
        audit_log.emit_audit_line({
            "audit": "smartsheet_oauth",
            "event_type": "audit_persist_failed",
            "outcome": "error",
            "error_code": "AUDIT_INSERT",
            "meta": {"hint": event_type},
        })


# ═══════════════════════════════════════════════════════════════════
#  Blueprint factory
# ═══════════════════════════════════════════════════════════════════

def create_oauth_blueprint(
    *,
    sb,
    crypto_key: bytes,
    resolve_base_url,
    user_member_of_org,
    verify_jwt,
    jwt_secret: str,
) -> Blueprint:
    bp = Blueprint("smartsheet_oauth", __name__)

    broker_secret = (os.getenv("SMARTSHEET_BROKER_SECRET") or "").strip()

    @bp.before_request
    def _throttle_oauth_routes():
        """Per-caller sliding-window limit on user-facing /oauth/* routes (API4)."""
        if "/oauth/" not in request.path:
            return None  # broker route has its own lockout in _require_broker
        claims = verify_jwt(request.headers.get("Authorization"), jwt_secret)
        caller = str(claims["sub"]) if claims and claims.get("sub") else f"ip:{_client_ip()}"
        if _rate_limited(f"oauth:{caller}", OAUTH_RATE_LIMIT_PER_MIN):
            rid = getattr(g, "encrypt_request_id", "")
            return jsonify(
                error="Too many requests. Try again shortly.",
                error_code="RATE_LIMITED", request_id=rid,
            ), 429
        return None

    # ── auth helpers ────────────────────────────────────────────────

    def _require_user() -> Tuple[Optional[str], Optional[Any]]:
        """Returns (user_id, None) or (None, flask error response)."""
        claims = verify_jwt(request.headers.get("Authorization"), jwt_secret)
        if not claims or not claims.get("sub"):
            rid = getattr(g, "encrypt_request_id", "")
            return None, (jsonify(
                error="Unauthorized",
                error_code="AUTH_REQUIRED",
                detail="Valid Authorization: Bearer session required.",
                request_id=rid,
            ), 401)
        user_id = str(claims["sub"])
        g.actor_user_id = user_id
        return user_id, None

    def _require_member(user_id: str, org_id: str) -> Optional[Any]:
        if not org_id or not user_member_of_org(user_id, str(org_id)):
            rid = getattr(g, "encrypt_request_id", "")
            return (jsonify(
                error="Forbidden",
                error_code="NOT_ORG_MEMBER",
                detail="Not a member of this organization.",
                request_id=rid,
            ), 403)
        return None

    def _require_broker() -> Optional[Any]:
        rid = getattr(g, "encrypt_request_id", "")
        # The broker hands out plaintext tokens: it must only ever exist on the
        # internal root path, never on the public /encrypt-prefixed registration.
        if request.path.startswith("/encrypt/"):
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="unauthorized",
                http_status=404, error_code="BROKER_PUBLIC_PATH",
            )
            return jsonify(error="Not found", request_id=rid), 404

        ip = _client_ip()
        if _rate_limited(f"broker-fail:{ip}", BROKER_AUTH_FAIL_LIMIT,
                         BROKER_AUTH_FAIL_WINDOW_SECONDS, record=False):
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="rate_limited",
                http_status=429, error_code="BROKER_AUTH_LOCKED",
            )
            return jsonify(error="Too many attempts", error_code="BROKER_AUTH_LOCKED",
                           request_id=rid), 429

        supplied = (request.headers.get("X-Broker-Secret") or "").strip()
        if not broker_secret or not supplied or not hmac.compare_digest(supplied, broker_secret):
            _rate_limited(f"broker-fail:{ip}", BROKER_AUTH_FAIL_LIMIT,
                          BROKER_AUTH_FAIL_WINDOW_SECONDS)  # record the failure
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="unauthorized",
                http_status=401, error_code="BROKER_AUTH_FAILED",
            )
            return jsonify(error="Unauthorized", error_code="BROKER_AUTH_FAILED", request_id=rid), 401
        return None

    # ── data access helpers ─────────────────────────────────────────

    def _get_connection(org_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        r = (
            sb.table("smartsheet_oauth_connections")
            .select("*")
            .eq("organization_id", org_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    def _get_token_row(connection_id: str, product_slug: str) -> Optional[Dict[str, Any]]:
        r = (
            sb.table("smartsheet_oauth_tokens")
            .select("*")
            .eq("connection_id", connection_id)
            .eq("product_slug", product_slug)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    def _persist_token_pair(
        *,
        connection_id: str,
        org_id: str,
        user_id: str,
        product_slug: str,
        token_body: Dict[str, Any],
        scopes: List[str],
    ) -> None:
        expires_in = int(token_body.get("expires_in") or 0)
        expires_at = _now() + timedelta(seconds=max(expires_in, 60))
        sb.table("smartsheet_oauth_tokens").upsert(
            {
                "connection_id": connection_id,
                "product_slug": product_slug,
                "encrypted_access_token": encrypt_bound(
                    crypto_key, token_body["access_token"], _aad(org_id, user_id, product_slug, "access")
                ),
                "encrypted_refresh_token": encrypt_bound(
                    crypto_key, token_body["refresh_token"], _aad(org_id, user_id, product_slug, "refresh")
                ),
                "expires_at": _iso(expires_at),
                "scopes": scopes,
                "status": "active",
                "status_detail": None,
                "last_refreshed_at": _iso(_now()),
                "refresh_claimed_at": None,
                "refresh_failure_count": 0,
            },
            on_conflict="connection_id,product_slug",
            ignore_duplicates=False,
        ).execute()

    # ── refresh (single-flight lease) ───────────────────────────────

    def _refresh_token_row(token_row: Dict[str, Any], conn: Dict[str, Any], rid: str) -> Optional[Dict[str, Any]]:
        """
        Refresh one token row with rotation. Returns the fresh row, or None if
        the refresh failed. Uses an optimistic lease on refresh_claimed_at so
        only one worker rotates at a time (rotation invalidates the old
        refresh token, so a race would orphan the pair).
        """
        org_id = conn["organization_id"]
        user_id = conn["user_id"]
        slug = token_row["product_slug"]
        lease_cutoff = _iso(_now() - timedelta(seconds=REFRESH_LEASE_SECONDS))

        claim = (
            sb.table("smartsheet_oauth_tokens")
            .update({"refresh_claimed_at": _iso(_now())})
            .eq("id", token_row["id"])
            .eq("status", "active")
            .or_(f"refresh_claimed_at.is.null,refresh_claimed_at.lt.{lease_cutoff}")
            .execute()
        )
        if not claim.data:
            # Another worker holds the lease — wait for it to finish and reread.
            for _ in range(20):
                time.sleep(0.5)
                fresh = _get_token_row(token_row["connection_id"], slug)
                if not fresh or fresh.get("status") != "active":
                    return None
                if fresh.get("refresh_claimed_at") is None and fresh.get("last_refreshed_at") != token_row.get("last_refreshed_at"):
                    return fresh
            return None

        try:
            base_url, _region_check = resolve_base_url(org_id)
            refresh_plain = decrypt_bound(
                crypto_key, token_row["encrypted_refresh_token"], _aad(org_id, user_id, slug, "refresh")
            )
            ok, body = refresh_token_pair(base_url, conn["ss_region"], refresh_plain)
            if not ok:
                failures = int(token_row.get("refresh_failure_count") or 0) + 1
                is_terminal = body.get("smartsheet_error_code") in (1004,) or failures >= 5
                sb.table("smartsheet_oauth_tokens").update({
                    "refresh_claimed_at": None,
                    "refresh_failure_count": failures,
                    "status": "error" if is_terminal else token_row.get("status", "active"),
                    "status_detail": f"refresh failed: {body.get('error_code')}",
                }).eq("id", token_row["id"]).execute()
                record_oauth_event(
                    sb, request_id=rid, event_type="refresh_failure", outcome="error",
                    organization_id=org_id, user_id=user_id, product_slug=slug,
                    error_code=body.get("error_code"),
                    meta={"failure_count": failures, "terminal": is_terminal},
                )
                return None

            _persist_token_pair(
                connection_id=token_row["connection_id"],
                org_id=org_id,
                user_id=user_id,
                product_slug=slug,
                token_body=body,
                scopes=token_row.get("scopes") or [],
            )
            record_oauth_event(
                sb, request_id=rid, event_type="refresh", outcome="success",
                organization_id=org_id, user_id=user_id, product_slug=slug,
            )
            return _get_token_row(token_row["connection_id"], slug)
        except Exception as e:
            try:
                sb.table("smartsheet_oauth_tokens").update({"refresh_claimed_at": None}).eq(
                    "id", token_row["id"]
                ).execute()
            except Exception:
                pass
            record_oauth_event(
                sb, request_id=rid, event_type="refresh_failure", outcome="error",
                organization_id=org_id, user_id=user_id, product_slug=slug,
                error_code="REFRESH_EXCEPTION", meta={"hint": type(e).__name__},
            )
            return None

    # ═══════════════════════════════════════════════════════════════
    #  POST /oauth/start
    # ═══════════════════════════════════════════════════════════════
    @bp.route("/oauth/start", methods=["POST"])
    def oauth_start():
        rid = getattr(g, "encrypt_request_id", "")
        user_id, err = _require_user()
        if err:
            return err

        payload = request.get_json(silent=True) or {}
        org_id = str(payload.get("organization_id") or "")
        product_slug = str(payload.get("product_slug") or "")
        return_path = str(payload.get("return_path") or "")[:500]
        # Same-site relative path only — never a scheme/host (open-redirect guard).
        if return_path and (
            not return_path.startswith("/")
            or return_path.startswith("//")
            or "\\" in return_path
            or ":" in return_path.split("?", 1)[0].split("#", 1)[0]
        ):
            return_path = ""

        if not org_id or not product_slug:
            return jsonify(error="organization_id and product_slug are required",
                           error_code="INVALID_PAYLOAD", request_id=rid), 400

        err = _require_member(user_id, org_id)
        if err:
            return err

        try:
            _base_url, ss_region = resolve_base_url(org_id)
        except RuntimeError as e:
            return jsonify(error=str(e), error_code="REGION_LOOKUP_FAILED", request_id=rid), 422

        region = (ss_region or "").upper()
        if region not in OAUTH_REGIONS:
            return jsonify(
                error=f"Smartsheet OAuth is not available for region {region}. Use an API key instead.",
                error_code="REGION_UNSUPPORTED", request_id=rid,
            ), 422

        client_id, client_secret = _client_creds(region)
        if not client_id or not client_secret:
            return jsonify(
                error=f"Smartsheet OAuth app is not configured for region {region}.",
                error_code="REGION_NOT_CONFIGURED", request_id=rid,
            ), 503

        # Cap pending states per user (cleanup of expired happens lazily here).
        # Fail closed: if the quota check itself errors we refuse rather than
        # silently dropping the only per-user cap on pending OAuth states.
        try:
            sb.table("smartsheet_oauth_states").delete().eq("user_id", user_id).lt(
                "expires_at", _iso(_now())
            ).execute()
            pending = (
                sb.table("smartsheet_oauth_states")
                .select("id", count="exact")
                .eq("user_id", user_id)
                .is_("consumed_at", "null")
                .execute()
            )
            if (pending.count or 0) >= MAX_PENDING_STATES_PER_USER:
                return jsonify(
                    error="A Smartsheet connection is already in progress. Finish or retry that flow first.",
                    error_code="OAUTH_ALREADY_IN_PROGRESS", request_id=rid,
                ), 429
        except Exception:
            return jsonify(
                error="Connection service is temporarily unavailable. Try again shortly.",
                error_code="STATE_QUOTA_UNAVAILABLE", request_id=rid,
            ), 503

        state_nonce = pysecrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state_nonce.encode()).hexdigest()
        sb.table("smartsheet_oauth_states").insert({
            "state_hash": state_hash,
            "user_id": user_id,
            "organization_id": org_id,
            "product_slug": product_slug,
            "return_path": return_path or None,
            "expires_at": _iso(_now() + timedelta(minutes=STATE_TTL_MINUTES)),
        }).execute()

        scopes = scopes_for_tool(product_slug)
        authorize_base = OAUTH_REGIONS[region]["authorize_base"]
        authorize_url = (
            f"{authorize_base}/b/authorize"
            f"?response_type=code"
            f"&client_id={client_id}"
            f"&scope={'%20'.join(scopes)}"
            f"&state={state_nonce}"
        )

        record_oauth_event(
            sb, request_id=rid, event_type="connect", outcome="started",
            organization_id=org_id, user_id=user_id, product_slug=product_slug,
            actor_user_id=user_id, meta={"region": region, "scopes": scopes},
        )
        return jsonify(
            authorize_url=authorize_url,
            state=state_nonce,
            region=region,
            scopes=scopes,
            expires_in_seconds=STATE_TTL_MINUTES * 60,
            request_id=rid,
        )

    # ═══════════════════════════════════════════════════════════════
    #  POST /oauth/exchange
    # ═══════════════════════════════════════════════════════════════
    @bp.route("/oauth/exchange", methods=["POST"])
    def oauth_exchange():
        rid = getattr(g, "encrypt_request_id", "")
        user_id, err = _require_user()
        if err:
            return err

        payload = request.get_json(silent=True) or {}
        code = str(payload.get("code") or "")
        state_nonce = str(payload.get("state") or "")
        if not code or not state_nonce:
            return jsonify(error="code and state are required",
                           error_code="INVALID_PAYLOAD", request_id=rid), 400

        # Validate + consume state atomically (single-use)
        state_hash = hashlib.sha256(state_nonce.encode()).hexdigest()
        consumed = (
            sb.table("smartsheet_oauth_states")
            .update({"consumed_at": _iso(_now())})
            .eq("state_hash", state_hash)
            .eq("user_id", user_id)
            .is_("consumed_at", "null")
            .gt("expires_at", _iso(_now()))
            .execute()
        )
        if not consumed.data:
            record_oauth_event(
                sb, request_id=rid, event_type="connect", outcome="state_rejected",
                http_status=400, user_id=user_id, actor_user_id=user_id,
                error_code="OAUTH_STATE_INVALID",
            )
            return jsonify(
                error="This authorization link is invalid or has expired. Please start the connection again.",
                error_code="OAUTH_STATE_INVALID", request_id=rid,
            ), 400

        state_row = consumed.data[0]
        org_id = state_row["organization_id"]
        product_slug = state_row["product_slug"]

        # Membership could have changed since /oauth/start (e.g. seat removed)
        err = _require_member(user_id, org_id)
        if err:
            return err

        try:
            base_url, ss_region = resolve_base_url(org_id)
        except RuntimeError as e:
            return jsonify(error=str(e), error_code="REGION_LOOKUP_FAILED", request_id=rid), 422
        region = (ss_region or "").upper()

        ok, body = exchange_authorization_code(base_url, region, code)
        if not ok:
            record_oauth_event(
                sb, request_id=rid, event_type="connect", outcome="exchange_failed",
                http_status=502, organization_id=org_id, user_id=user_id,
                product_slug=product_slug, actor_user_id=user_id,
                error_code=body.get("error_code"), meta=body,
            )
            return jsonify(
                error="Smartsheet did not accept the authorization. Please try connecting again.",
                error_code=body.get("error_code", "OAUTH_EXCHANGE_FAILED"), request_id=rid,
            ), 502

        # Verify the token and capture the Smartsheet identity
        ss_user = fetch_smartsheet_user(base_url, body["access_token"])
        if not ss_user:
            record_oauth_event(
                sb, request_id=rid, event_type="connect", outcome="verify_failed",
                http_status=502, organization_id=org_id, user_id=user_id,
                product_slug=product_slug, actor_user_id=user_id,
                error_code="OAUTH_VERIFY_FAILED",
            )
            return jsonify(
                error="Connected to Smartsheet but could not verify the account. Please try again.",
                error_code="OAUTH_VERIFY_FAILED", request_id=rid,
            ), 502

        scopes = scopes_for_tool(product_slug)

        # Upsert connection, then the per-tool token pair
        existing = _get_connection(org_id, user_id)
        conn_fields = {
            "organization_id": org_id,
            "user_id": user_id,
            "ss_region": region,
            "smartsheet_user_id": ss_user.get("id"),
            "smartsheet_user_email": ss_user.get("email"),
            "status": "active",
            "status_detail": None,
            "last_verified_at": _iso(_now()),
        }
        if existing:
            merged_scopes = sorted(set(existing.get("consented_scopes") or []) | set(scopes))
            conn_fields["consented_scopes"] = merged_scopes
            sb.table("smartsheet_oauth_connections").update(conn_fields).eq("id", existing["id"]).execute()
            connection_id = existing["id"]
        else:
            conn_fields["consented_scopes"] = sorted(set(scopes))
            created = sb.table("smartsheet_oauth_connections").insert(conn_fields).execute()
            connection_id = created.data[0]["id"]

        _persist_token_pair(
            connection_id=connection_id,
            org_id=org_id,
            user_id=user_id,
            product_slug=product_slug,
            token_body=body,
            scopes=scopes,
        )

        record_oauth_event(
            sb, request_id=rid, event_type="token_mint", outcome="success",
            organization_id=org_id, user_id=user_id, product_slug=product_slug,
            actor_user_id=user_id,
            meta={"region": region, "smartsheet_email": ss_user.get("email")},
        )
        return jsonify(
            status="ok",
            product_slug=product_slug,
            connection={
                "smartsheet_user_email": ss_user.get("email"),
                "region": region,
            },
            return_path=state_row.get("return_path"),
            request_id=rid,
        )

    # ═══════════════════════════════════════════════════════════════
    #  POST /oauth/disconnect
    # ═══════════════════════════════════════════════════════════════
    @bp.route("/oauth/disconnect", methods=["POST"])
    def oauth_disconnect():
        rid = getattr(g, "encrypt_request_id", "")
        user_id, err = _require_user()
        if err:
            return err

        payload = request.get_json(silent=True) or {}
        org_id = str(payload.get("organization_id") or "")
        product_slug = str(payload.get("product_slug") or "").strip() or None
        disconnect_all = payload.get("disconnect_all") is True
        if not org_id:
            return jsonify(error="organization_id is required",
                           error_code="INVALID_PAYLOAD", request_id=rid), 400
        # Full disconnect is destructive (revokes every tool's pair). It must
        # be requested explicitly — a missing/empty product_slug alone must
        # never wipe the whole connection.
        if not product_slug and not disconnect_all:
            return jsonify(
                error="product_slug is required to revoke a single tool; "
                      "send disconnect_all=true to disconnect every tool.",
                error_code="DISCONNECT_SCOPE_REQUIRED", request_id=rid,
            ), 400

        err = _require_member(user_id, org_id)
        if err:
            return err

        conn = _get_connection(org_id, user_id)
        if not conn:
            return jsonify(status="ok", detail="No connection to disconnect.", request_id=rid)

        # ── Per-tool revoke: only this tool's token pair, connection stays ──
        if product_slug:
            row = _get_token_row(conn["id"], product_slug)
            if not row or row.get("status") != "active":
                return jsonify(status="ok", detail="Tool is not authorized.", request_id=rid)

            revoked_at_smartsheet = False
            try:
                base_url, _region = resolve_base_url(org_id)
                access_plain = decrypt_bound(
                    crypto_key, row["encrypted_access_token"],
                    _aad(org_id, user_id, product_slug, "access"),
                )
                revoked_at_smartsheet = revoke_single_token(base_url, access_plain)
            except Exception:
                pass

            sb.table("smartsheet_oauth_tokens").update({
                "status": "revoked",
                "status_detail": "user revoked tool",
                "encrypted_access_token": "",
                "encrypted_refresh_token": "",
                "refresh_claimed_at": None,
            }).eq("id", row["id"]).execute()

            record_oauth_event(
                sb, request_id=rid, event_type="revoke", outcome="success",
                organization_id=org_id, user_id=user_id, product_slug=product_slug,
                actor_user_id=user_id,
                meta={"revoked_at_smartsheet": revoked_at_smartsheet, "scope": "single_tool"},
            )
            return jsonify(
                status="ok", product_slug=product_slug,
                revoked_at_smartsheet=revoked_at_smartsheet, request_id=rid,
            )

        # ── Full disconnect: every tool + the connection itself ─────────
        # Best-effort Smartsheet-side revocation using any live access token.
        revoked_at_smartsheet = False
        try:
            base_url, _region = resolve_base_url(org_id)
            tokens = (
                sb.table("smartsheet_oauth_tokens")
                .select("*")
                .eq("connection_id", conn["id"])
                .eq("status", "active")
                .execute()
            ).data or []
            for row in tokens:
                try:
                    access_plain = decrypt_bound(
                        crypto_key, row["encrypted_access_token"],
                        _aad(org_id, user_id, row["product_slug"], "access"),
                    )
                    if revoke_all_tokens(base_url, access_plain):
                        revoked_at_smartsheet = True
                        break  # deleteAllForApiClient revokes every pair at once
                except Exception:
                    continue
        except Exception:
            pass

        sb.table("smartsheet_oauth_tokens").update({
            "status": "revoked",
            "status_detail": "user disconnect",
            "encrypted_access_token": "",
            "encrypted_refresh_token": "",
        }).eq("connection_id", conn["id"]).execute()
        sb.table("smartsheet_oauth_connections").update({
            "status": "revoked",
            "status_detail": "user disconnect",
        }).eq("id", conn["id"]).execute()

        record_oauth_event(
            sb, request_id=rid, event_type="disconnect", outcome="success",
            organization_id=org_id, user_id=user_id, actor_user_id=user_id,
            meta={"revoked_at_smartsheet": revoked_at_smartsheet},
        )
        return jsonify(status="ok", revoked_at_smartsheet=revoked_at_smartsheet, request_id=rid)

    # ═══════════════════════════════════════════════════════════════
    #  GET /oauth/status
    # ═══════════════════════════════════════════════════════════════
    @bp.route("/oauth/status", methods=["GET"])
    def oauth_status():
        rid = getattr(g, "encrypt_request_id", "")
        user_id, err = _require_user()
        if err:
            return err

        org_id = str(request.args.get("organization_id") or "")
        if not org_id:
            return jsonify(error="organization_id is required",
                           error_code="INVALID_PAYLOAD", request_id=rid), 400
        err = _require_member(user_id, org_id)
        if err:
            return err

        try:
            _base_url, ss_region = resolve_base_url(org_id)
            region = (ss_region or "").upper()
        except RuntimeError:
            region = None

        oauth_supported = region in OAUTH_REGIONS and all(_client_creds(region))

        conn = _get_connection(org_id, user_id)
        if not conn or conn.get("status") != "active":
            return jsonify(
                oauth_supported=oauth_supported,
                region=region,
                connection=None,
                tools=[],
                request_id=rid,
            )

        tokens = (
            sb.table("smartsheet_oauth_tokens")
            .select("product_slug, status, status_detail, expires_at, last_refreshed_at, scopes")
            .eq("connection_id", conn["id"])
            .execute()
        ).data or []

        return jsonify(
            oauth_supported=oauth_supported,
            region=region,
            connection={
                "smartsheet_user_email": conn.get("smartsheet_user_email"),
                "status": conn.get("status"),
                "connected_at": conn.get("connected_at"),
                "last_verified_at": conn.get("last_verified_at"),
            },
            tools=_tools_status_payload(tokens),
            request_id=rid,
        )

    # ═══════════════════════════════════════════════════════════════
    #  POST /internal/token/resolve  (token broker for tool backends)
    # ═══════════════════════════════════════════════════════════════
    @bp.route("/internal/token/resolve", methods=["POST"])
    def token_resolve():
        """Resolve credentials with an explicit OAuth-state contract (SH-182).

        Every response carries ``oauth_state`` so consumers (SH-178 runner
        failover) can tell a healthy runner identity from transition-PAT
        continuity on a revoked one:

          state      meaning
          ─────────  ────────────────────────────────────────────────────────
          active     healthy per-user OAuth token was returned (source=oauth)
          revoked    the user's grant/connection was revoked
          error      terminal refresh failure (needs reconnect)
          missing    no OAuth grant was ever established (rollout state)
          unknown    transient failure — NOT a revocation signal

        Resolution matrix:
          1. healthy OAuth            → 200 source=oauth, oauth_state=active
          2. revoked  + PAT           → 200 source=pat, oauth_state=revoked,
                                        fallback_used=true, CONNECTION_REVOKED
          3. terminal + PAT           → 200 source=pat, oauth_state=error,
                                        fallback_used=true, TOKEN_REFRESH_FAILED
          4. no grant + PAT           → 200 source=pat, oauth_state=missing,
                                        fallback_used=true, NO_OAUTH_TOKEN
          5. revoked/terminal, no PAT → 409 CONNECTION_REVOKED | TOKEN_REFRESH_FAILED
          6. no grant, no PAT         → 404 NO_CREDENTIALS
          7. transient,   no PAT      → 503 OAUTH_TEMPORARILY_UNAVAILABLE
        """
        rid = getattr(g, "encrypt_request_id", "")
        err = _require_broker()
        if err:
            return err

        payload = request.get_json(silent=True) or {}
        org_id = str(payload.get("organization_id") or "")
        user_id = str(payload.get("user_id") or "")
        product_slug = str(payload.get("product_slug") or "")
        force_refresh = bool(payload.get("force_refresh"))

        if not org_id or not product_slug:
            return jsonify(error="organization_id and product_slug are required",
                           error_code="INVALID_PAYLOAD", request_id=rid), 400

        try:
            base_url, ss_region = resolve_base_url(org_id)
        except RuntimeError as e:
            return jsonify(error=str(e), error_code="REGION_LOOKUP_FAILED", request_id=rid), 422

        # ── 1. Per-user OAuth token (preferred) ──────────────────────
        # A per-user token is only released when the user actually belongs to
        # the org (defence-in-depth against spoofed user ids from tool
        # backends running with legacy header auth). PAT fallback below is
        # org-scoped and unaffected.
        if user_id and not user_member_of_org(user_id, org_id):
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="not_member",
                organization_id=org_id, user_id=user_id, product_slug=product_slug,
                error_code="NOT_ORG_MEMBER",
            )
            user_id = ""

        oauth_state = "missing"
        fallback_reason = "NO_OAUTH_TOKEN"
        conn = None

        if user_id:
            conn = _get_connection(org_id, user_id)
            if conn and conn.get("status") == "active":
                row = _get_token_row(conn["id"], product_slug)
                if row and row.get("status") == "active":
                    expires_at = _parse_ts(row.get("expires_at"))
                    needs_refresh = force_refresh or (
                        expires_at is not None
                        and expires_at <= _now() + timedelta(seconds=RESOLVE_REFRESH_THRESHOLD_SECONDS)
                    )
                    if needs_refresh:
                        refreshed = _refresh_token_row(row, conn, rid)
                        if refreshed is None:
                            # Refresh failed — re-read the row so a terminal
                            # outcome (status flipped by _refresh_token_row)
                            # is distinguished from a transient one.
                            current = _get_token_row(conn["id"], product_slug) or {}
                            status_now = current.get("status")
                            if status_now == "error":
                                oauth_state, fallback_reason = "error", "TOKEN_REFRESH_FAILED"
                            elif status_now == "revoked":
                                oauth_state, fallback_reason = "revoked", "CONNECTION_REVOKED"
                            else:
                                oauth_state, fallback_reason = "unknown", "REFRESH_UNAVAILABLE"
                        row = refreshed
                    if row and row.get("status") == "active":
                        try:
                            access_plain = decrypt_bound(
                                crypto_key, row["encrypted_access_token"],
                                _aad(org_id, user_id, product_slug, "access"),
                            )
                            record_oauth_event(
                                sb, request_id=rid, event_type="resolve", outcome="success",
                                organization_id=org_id, user_id=user_id, product_slug=product_slug,
                                meta={"source": "oauth", "oauth_state": "active"},
                            )
                            return jsonify(
                                source="oauth",
                                oauth_state="active",
                                fallback_used=False,
                                access_token=access_plain,
                                base_url=base_url,
                                region=(ss_region or "").upper(),
                                expires_at=row.get("expires_at"),
                                smartsheet_user_email=conn.get("smartsheet_user_email"),
                                request_id=rid,
                            )
                        except Exception:
                            record_oauth_event(
                                sb, request_id=rid, event_type="resolve_denied", outcome="error",
                                organization_id=org_id, user_id=user_id, product_slug=product_slug,
                                error_code="DECRYPT_FAILED",
                            )
                            # Crypto/data problem, not a revocation: report as
                            # transient so consumers don't trigger failover.
                            oauth_state, fallback_reason = "unknown", "DECRYPT_FAILED"
                elif row and row.get("status") == "revoked":
                    oauth_state, fallback_reason = "revoked", "CONNECTION_REVOKED"
                    record_oauth_event(
                        sb, request_id=rid, event_type="resolve_denied", outcome="token_unhealthy",
                        organization_id=org_id, user_id=user_id, product_slug=product_slug,
                        error_code="CONNECTION_REVOKED",
                    )
                elif row:  # status == "error" — terminal refresh failure
                    oauth_state, fallback_reason = "error", "TOKEN_REFRESH_FAILED"
                    record_oauth_event(
                        sb, request_id=rid, event_type="resolve_denied", outcome="token_unhealthy",
                        organization_id=org_id, user_id=user_id, product_slug=product_slug,
                        error_code="TOKEN_REFRESH_FAILED",
                    )
            elif conn:  # whole connection revoked/errored (disconnect_all)
                oauth_state, fallback_reason = "revoked", "CONNECTION_REVOKED"
                record_oauth_event(
                    sb, request_id=rid, event_type="resolve_denied", outcome="token_unhealthy",
                    organization_id=org_id, user_id=user_id, product_slug=product_slug,
                    error_code="CONNECTION_REVOKED",
                )

        # ── 2. Legacy org PAT fallback (dual-mode GOV/AUS + transition) ──
        # The PAT keeps work running, but the response must say *why* it was
        # used: oauth_state=revoked/error here is the failover signal SH-178
        # consumes. Only non-secret identity metadata is included.
        pat = (
            sb.table("smartsheet_tokens")
            .select("encrypted_token")
            .eq("organization_id", org_id)
            .eq("product_slug", product_slug)
            .limit(1)
            .execute()
        )
        if pat.data and pat.data[0].get("encrypted_token"):
            try:
                # Legacy ciphertexts have no AAD binding
                blob = base64.b64decode(pat.data[0]["encrypted_token"])
                pat_plain = AESGCM(crypto_key).decrypt(blob[:12], blob[12:], None).decode()
                record_oauth_event(
                    sb, request_id=rid, event_type="resolve", outcome="success",
                    organization_id=org_id, user_id=user_id or None, product_slug=product_slug,
                    meta={"source": "pat", "oauth_state": oauth_state,
                          "fallback_reason": fallback_reason},
                )
                return jsonify(
                    source="pat",
                    oauth_state=oauth_state,
                    fallback_used=True,
                    fallback_reason=fallback_reason,
                    access_token=pat_plain,
                    base_url=base_url,
                    region=(ss_region or "").upper(),
                    expires_at=None,
                    smartsheet_user_email=(conn or {}).get("smartsheet_user_email"),
                    request_id=rid,
                )
            except Exception:
                record_oauth_event(
                    sb, request_id=rid, event_type="resolve_denied", outcome="error",
                    organization_id=org_id, user_id=user_id or None, product_slug=product_slug,
                    error_code="PAT_DECRYPT_FAILED",
                )

        # ── 3. No usable credentials — typed by OAuth state ──────────
        if oauth_state in ("revoked", "error"):
            # Authoritative conflict: the grant is gone and there is no
            # transition PAT. Never collapsed into NO_CREDENTIALS so the
            # shared resolver can raise a typed error (no silent fallback).
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="revoked_no_fallback",
                organization_id=org_id, user_id=user_id or None, product_slug=product_slug,
                error_code=fallback_reason, meta={"oauth_state": oauth_state},
            )
            return jsonify(
                error="Smartsheet OAuth connection is revoked or terminally failed "
                      "and no transition PAT is available. Reconnect the tool.",
                error_code=fallback_reason,
                oauth_state=oauth_state,
                request_id=rid,
            ), 409

        if oauth_state == "unknown":
            # Transient — must never be reported as a revocation.
            record_oauth_event(
                sb, request_id=rid, event_type="resolve_denied", outcome="transient",
                organization_id=org_id, user_id=user_id or None, product_slug=product_slug,
                error_code="OAUTH_TEMPORARILY_UNAVAILABLE",
                meta={"fallback_reason": fallback_reason},
            )
            return jsonify(
                error="Smartsheet OAuth token is temporarily unavailable. Retry shortly.",
                error_code="OAUTH_TEMPORARILY_UNAVAILABLE",
                oauth_state=oauth_state,
                request_id=rid,
            ), 503

        record_oauth_event(
            sb, request_id=rid, event_type="resolve_denied", outcome="no_credentials",
            organization_id=org_id, user_id=user_id or None, product_slug=product_slug,
            error_code="NO_CREDENTIALS",
        )
        return jsonify(
            error="No Smartsheet credentials available for this tool.",
            error_code="NO_CREDENTIALS",
            oauth_state=oauth_state,
            request_id=rid,
        ), 404

    # ═══════════════════════════════════════════════════════════════
    #  Background refresh sweep
    # ═══════════════════════════════════════════════════════════════
    def _refresh_sweep_once() -> int:
        """Refresh active tokens expiring inside the window. Returns count refreshed."""
        window_hours = int(os.getenv("SMARTSHEET_OAUTH_REFRESH_WINDOW_HOURS", "24"))
        cutoff = _iso(_now() + timedelta(hours=window_hours))
        rid = f"sweep-{pysecrets.token_hex(6)}"
        refreshed = 0
        try:
            rows = (
                sb.table("smartsheet_oauth_tokens")
                .select("*")
                .eq("status", "active")
                .lt("expires_at", cutoff)
                .limit(200)
                .execute()
            ).data or []
        except Exception as e:
            audit_log.emit_audit_line({
                "audit": "smartsheet_oauth", "event_type": "refresh_sweep",
                "outcome": "query_failed", "error_code": type(e).__name__,
            })
            return 0

        for row in rows:
            try:
                conn_r = (
                    sb.table("smartsheet_oauth_connections")
                    .select("*")
                    .eq("id", row["connection_id"])
                    .limit(1)
                    .execute()
                )
                conn = conn_r.data[0] if conn_r.data else None
                if not conn or conn.get("status") != "active":
                    continue
                if _refresh_token_row(row, conn, rid):
                    refreshed += 1
            except Exception:
                continue

        if rows:
            audit_log.emit_audit_line({
                "audit": "smartsheet_oauth", "event_type": "refresh_sweep",
                "outcome": "success", "meta": {"candidates": len(rows), "refreshed": refreshed},
            })
        return refreshed

    def _sweep_loop():
        interval = int(os.getenv("SMARTSHEET_OAUTH_REFRESH_SWEEP_SECONDS", "900"))
        while True:
            time.sleep(interval)
            try:
                _refresh_sweep_once()
            except Exception:
                pass

    def start_refresh_sweep():
        t = threading.Thread(target=_sweep_loop, name="ss-oauth-refresh-sweep", daemon=True)
        t.start()

    bp.start_refresh_sweep = start_refresh_sweep
    bp.refresh_sweep_once = _refresh_sweep_once
    return bp
