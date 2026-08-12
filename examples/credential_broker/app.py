import os
import sys
import base64
import secrets
import requests
from datetime import datetime, timezone

# Windows dev consoles default to cp1252, which can't encode the emoji used
# in log lines — reconfigure to UTF-8 so local runs don't crash on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
from flask import Flask, request, jsonify, g
from dotenv import load_dotenv
from supabase import create_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask_cors import CORS

from jwt_auth import verify_supabase_jwt
import audit_log
from oauth_smartsheet import create_oauth_blueprint

# Load environment variables
load_dotenv()

SB_URL = os.getenv("SUPABASE_URL")
SB_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
APP_CRYPTO_KEY = os.getenv("APP_CRYPTO_KEY")

_VERBOSE = os.getenv("ENCRYPT_TOOL_VERBOSE_LOGS", "true").lower() in ("1", "true", "yes")
_REQUIRE_JWT = os.getenv("ENCRYPT_TOOL_REQUIRE_JWT", "").lower() in ("1", "true", "yes")
_JWT_SECRET = (os.getenv("SUPABASE_JWT_SECRET") or "").strip()

try:
    _MAX_BODY = int(os.getenv("ENCRYPT_TOOL_MAX_BODY_BYTES", "262144"))
except ValueError:
    _MAX_BODY = 262144

if not all((SB_URL, SB_KEY, APP_CRYPTO_KEY)):
    raise RuntimeError("SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and APP_CRYPTO_KEY must be set in .env or environment.")

if _REQUIRE_JWT and not _JWT_SECRET:
    raise RuntimeError("ENCRYPT_TOOL_REQUIRE_JWT is set but SUPABASE_JWT_SECRET is missing.")

# Validate and decode the master key
try:
    _KEY = base64.b64decode(APP_CRYPTO_KEY, validate=True)
    if len(_KEY) != 32:
        raise ValueError
except Exception:
    raise RuntimeError("APP_CRYPTO_KEY must be a 32-byte base64 string.")

_IS_PROD = os.getenv("ENVIRONMENT", "").lower() == "production"


def _vlog(*args, **kwargs):
    if _VERBOSE:
        print(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════════
#  AES-GCM Encryption
# ═══════════════════════════════════════════════════════════════════
def encrypt_token(plain: str) -> str:
    """Encrypt plaintext → base64( IV(12B) || TAG(16B) || CIPHERTEXT )."""
    iv = secrets.token_bytes(12)  # 96-bit nonce
    aead = AESGCM(_KEY)
    ct = aead.encrypt(iv, plain.encode(), None)  # TAG||CIPHERTEXT
    blob = iv + ct
    return base64.b64encode(blob).decode()


# ═══════════════════════════════════════════════════════════════════
#  Regional Base URL Resolution
# ═══════════════════════════════════════════════════════════════════
def resolve_base_url(org_id: str) -> tuple:
    """
    Look up the Smartsheet regional base URL and ss_region for the org.

    Chain: organizations.ss_region → smartsheet_regions.base_url
    (exposed via the ``organization_ss_api_base`` view).

    Both values MUST exist.  A missing region or base_url is a data-integrity
    problem — falling back silently to US would let a EU/GOV/AUS token get
    validated against the wrong API and then fail at runtime.
    """
    try:
        org_row = sb.table("organizations").select("ss_region").eq("id", org_id).single().execute().data
    except Exception as e:
        raise RuntimeError(
            f"Could not look up organization {org_id}: {e}.  "
            "Verify the organization exists in the organizations table."
        )

    ss_region = org_row.get("ss_region") if org_row else None
    if not ss_region:
        raise RuntimeError(
            f"Organization {org_id} has no ss_region set.  "
            "Set it under the organization's plan settings (links to smartsheet_regions)."
        )

    try:
        url_row = (
            sb.table("organization_ss_api_base")
            .select("base_url")
            .eq("organization_id", org_id)
            .single()
            .execute()
            .data
        )
    except Exception as e:
        if "0 rows" in str(e):
            raise RuntimeError(
                f"organization_ss_api_base has no row for org {org_id} (ss_region={ss_region}).  "
                "Verify the region value exists in smartsheet_regions."
            )
        raise RuntimeError(f"organization_ss_api_base query failed for org {org_id}: {e}")

    base_url = url_row.get("base_url") if url_row else None
    if not base_url:
        raise RuntimeError(
            f"organization_ss_api_base returned NULL base_url for org {org_id} "
            f"(ss_region={ss_region}).  Check the smartsheet_regions table."
        )

    return base_url, ss_region


# ═══════════════════════════════════════════════════════════════════
#  Smartsheet Token Validation (region-aware)
# ═══════════════════════════════════════════════════════════════════
def verify_smartsheet_token(api_token: str, base_url: str = "https://api.smartsheet.com/2.0"):
    """
    Validates a Smartsheet API token by calling /users/me on the
    correct regional endpoint.  Returns { valid, error?, current_user? }.
    """
    base = base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        _vlog(f"🔍 Verifying Smartsheet API token against {base} …")

        # ── Step 1: /users/me (lightweight auth check) ──────────
        user_response = requests.get(
            f"{base}/users/me",
            headers=headers,
            timeout=12,
        )

        if user_response.status_code == 401:
            _vlog("❌ Smartsheet returned 401 – token is invalid or expired")
            return {
                "valid": False,
                "error": (
                    "Invalid API key. The token was rejected by Smartsheet. "
                    "Please generate a new API key from your Smartsheet account."
                ),
            }

        if user_response.status_code == 403:
            _vlog("❌ Smartsheet returned 403 – insufficient permissions")
            return {
                "valid": False,
                "error": (
                    "API key lacks required permissions. Ensure you are using "
                    "an API key with full access from your Smartsheet account settings."
                ),
            }

        if user_response.status_code != 200:
            body_preview = user_response.text[:300]
            _vlog(f"❌ Smartsheet returned unexpected status {user_response.status_code}: {body_preview}")
            return {
                "valid": False,
                "error": (
                    f"Smartsheet API returned status {user_response.status_code}. "
                    "This may indicate a region mismatch — please verify your plan's "
                    "Smartsheet region is set correctly, or contact your system admin."
                ),
            }

        user_data = user_response.json()
        email = user_data.get("email", "Unknown")
        _vlog(f"✅ Token verified for user: {email}")

        # ── Step 2 (optional): Fetch users list ─────────────────
        users_data = []
        try:
            users_resp = requests.get(f"{base}/users", headers=headers, timeout=15)
            if users_resp.status_code == 200:
                users_data = users_resp.json().get("data", [])
                _vlog(f"✅ Retrieved {len(users_data)} Smartsheet users")
            else:
                _vlog(f"⚠️  Could not fetch users list (Status {users_resp.status_code})")
        except Exception as ue:
            _vlog(f"⚠️  Users fetch skipped: {ue}")

        # ── Step 3 (optional): Fetch sheets count ───────────────
        sheets_count = 0
        try:
            sheets_resp = requests.get(
                f"{base}/sheets?includeAll=false&pageSize=1",
                headers=headers,
                timeout=15,
            )
            if sheets_resp.status_code == 200:
                sheets_json = sheets_resp.json()
                sheets_count = sheets_json.get("totalCount", len(sheets_json.get("data", [])))
                _vlog(f"✅ User has access to {sheets_count} sheets")
        except Exception as se:
            _vlog(f"⚠️  Sheets count skipped: {se}")

        return {
            "valid": True,
            "current_user": {
                "id": user_data.get("id"),
                "email": email,
                "firstName": user_data.get("firstName"),
                "lastName": user_data.get("lastName"),
                "admin": user_data.get("admin", False),
            },
            "users": users_data,
            "sheets_count": sheets_count,
            "account_verified": True,
        }

    except requests.exceptions.Timeout:
        _vlog("❌ Smartsheet API request timed out")
        return {
            "valid": False,
            "error": "Smartsheet API request timed out. Please check your connection and try again.",
        }
    except requests.exceptions.RequestException as e:
        _vlog(f"❌ Network error reaching Smartsheet: {e}")
        return {
            "valid": False,
            "error": f"Could not reach Smartsheet API: {e}",
        }
    except Exception as e:
        _vlog(f"❌ Unexpected error during verification: {e}")
        return {
            "valid": False,
            "error": f"Verification failed: {e}",
        }


# ═══════════════════════════════════════════════════════════════════
#  Supabase & Flask
# ═══════════════════════════════════════════════════════════════════
sb = create_client(SB_URL, SB_KEY)


def user_member_of_org(user_id: str, org_id: str) -> bool:
    try:
        r = (
            sb.table("user_organization_memberships")
            .select("user_id")
            .eq("user_id", user_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = _MAX_BODY

# CORS configuration
allowed_origins = os.getenv("ENCRYPT_TOOL_ALLOWED_ORIGINS", "http://localhost:3000")
origins_list = [o.strip() for o in allowed_origins.split(",") if o.strip()]
CORS(app, resources={r"/*": {"origins": origins_list}}, supports_credentials=True)


# ═══════════════════════════════════════════════════════════════════
#  Smartsheet OAuth (connections, token broker, refresh sweep)
# ═══════════════════════════════════════════════════════════════════
# OAuth endpoints always require a Supabase JWT (unlike /encrypt where the
# gate is env-controlled): they establish and expose per-user credentials.
_oauth_bp = create_oauth_blueprint(
    sb=sb,
    crypto_key=_KEY,
    resolve_base_url=resolve_base_url,
    user_member_of_org=user_member_of_org,
    verify_jwt=verify_supabase_jwt,
    jwt_secret=_JWT_SECRET,
)
app.register_blueprint(_oauth_bp)
# Also reachable under /encrypt/* so the existing ALB path rule
# (api.prodactivelabs.com/encrypt → this service) covers OAuth routes.
app.register_blueprint(_oauth_bp, url_prefix="/encrypt", name="smartsheet_oauth_under_encrypt")
if os.getenv("SMARTSHEET_OAUTH_REFRESH_SWEEP", "1").lower() in ("1", "true", "yes"):
    _oauth_bp.start_refresh_sweep()


@app.before_request
def _assign_request_context():
    g.encrypt_request_id = audit_log.new_request_id(request.headers.get("X-Request-ID"))
    g.actor_user_id = None


@app.after_request
def _audit_response_headers(response):
    rid = getattr(g, "encrypt_request_id", None)
    if rid:
        response.headers["X-Request-Id"] = rid
    return response


@app.errorhandler(413)
def _payload_too_large(_e):
    rid = getattr(g, "encrypt_request_id", audit_log.new_request_id(None))
    audit_log.record_encrypt_event(
        sb,
        request_id=rid,
        event_type="encrypt_request",
        outcome="payload_too_large",
        http_status=413,
        error_code="payload_too_large",
        meta={"max_bytes": _MAX_BODY},
    )
    return jsonify(
        error="Payload too large",
        detail=f"Max body size is {_MAX_BODY} bytes.",
        request_id=rid,
    ), 413


# ═══════════════════════════════════════════════════════════════════
#  /encrypt  — Validate → Encrypt → Persist
# ═══════════════════════════════════════════════════════════════════
@app.route("/encrypt", methods=["POST"])
def encrypt_route():
    rid = getattr(g, "encrypt_request_id", audit_log.new_request_id(None))
    org_id = None
    slug = None
    key_kind = None

    try:
        payload = request.get_json(force=True)
        org_id = payload["organization_id"]
        plain = payload["plain_token"]
        slug = payload.get("product_slug") or payload.get("integration_slug")
        is_integration = "integration_slug" in payload
        key_kind = "integration" if is_integration else "product"

        _vlog("🔐 Encrypt request received:")
        _vlog(f"   Organization ID: {org_id}")
        _vlog(f"   {'Integration' if is_integration else 'Product'} Slug: {slug}")
        _vlog(f"   Token length: {len(plain)}")

    except Exception as e:
        audit_log.record_encrypt_event(
            sb,
            request_id=rid,
            event_type="encrypt_request",
            outcome="bad_request",
            http_status=400,
            organization_id=str(org_id) if org_id else None,
            key_kind=key_kind,
            slug=slug,
            error_code="invalid_payload",
            meta={"hint": type(e).__name__},
        )
        return jsonify(error="invalid JSON payload", request_id=rid), 400

    if _REQUIRE_JWT:
        claims = verify_supabase_jwt(request.headers.get("Authorization"), _JWT_SECRET)
        if not claims:
            audit_log.record_encrypt_event(
                sb,
                request_id=rid,
                event_type="encrypt_auth",
                outcome="unauthorized",
                http_status=401,
                organization_id=str(org_id),
                key_kind=key_kind,
                slug=slug,
                error_code="missing_or_invalid_jwt",
            )
            return jsonify(
                error="Unauthorized",
                detail="Valid Authorization: Bearer session required.",
                request_id=rid,
            ), 401
        user_id = claims.get("sub")
        g.actor_user_id = str(user_id) if user_id else None
        if not user_id or not user_member_of_org(str(user_id), str(org_id)):
            audit_log.record_encrypt_event(
                sb,
                request_id=rid,
                event_type="encrypt_auth",
                outcome="forbidden",
                http_status=403,
                organization_id=str(org_id),
                key_kind=key_kind,
                slug=slug,
                actor_user_id=str(user_id) if user_id else None,
                error_code="not_org_member",
            )
            return jsonify(
                error="Forbidden",
                detail="Not a member of this organization.",
                request_id=rid,
            ), 403

    try:
        # ── 1. Resolve the Smartsheet regional base URL ───────────
        base_url, ss_region = resolve_base_url(org_id)
        _vlog(f"🌐 Region={ss_region}  base_url={base_url}")

        # ── 2. Validate the API token against Smartsheet ──────────
        _vlog(f"🔍 Validating Smartsheet token for slug={slug} …")
        verification_result = verify_smartsheet_token(plain, base_url=base_url)

        if not verification_result.get("valid"):
            error_msg = verification_result.get("error", "Token verification failed")
            _vlog(f"❌ Token validation FAILED: {error_msg}")
            detail = (
                f"{error_msg}  |  "
                f"Plan region: {ss_region}.  "
                "If this is unexpected, check that your plan's Smartsheet region "
                "is correct, try generating a new API key, or contact your system administrator."
            )
            audit_log.record_encrypt_event(
                sb,
                request_id=rid,
                event_type="smartsheet_validation",
                outcome="token_rejected",
                http_status=401,
                organization_id=str(org_id),
                key_kind=key_kind,
                slug=slug,
                actor_user_id=getattr(g, "actor_user_id", None),
                error_code="smartsheet_401_or_region",
                meta={"ss_region": ss_region},
            )
            return jsonify(error=detail, verified=False, request_id=rid), 401

        verified_email = verification_result["current_user"].get("email", "—")
        _vlog(
            f"✅ Token VERIFIED  user={verified_email}  sheets={verification_result.get('sheets_count', 0)}"
            if _VERBOSE
            else "✅ Token VERIFIED"
        )

        # ── 3. Encrypt the validated token ────────────────────────
        b64_ct = encrypt_token(plain)
        _vlog("✅ Token encrypted (AES-GCM)")

        # ── 4. Verify organization exists ─────────────────────────
        org_check = sb.table("organizations").select("id, name, plan_type").eq("id", org_id).single().execute()
        if not org_check.data:
            audit_log.record_encrypt_event(
                sb,
                request_id=rid,
                event_type="encrypt_request",
                outcome="org_not_found",
                http_status=404,
                organization_id=str(org_id),
                key_kind=key_kind,
                slug=slug,
                actor_user_id=getattr(g, "actor_user_id", None),
                error_code="organization_missing",
            )
            return jsonify(error="Organization not found", request_id=rid), 404
        _vlog(f"🏢 Organization: {org_check.data.get('name')}")

        # ── 5. Persist to the correct table ───────────────────────
        if is_integration:
            _vlog("💾 Saving integration key to all_integration_api_keys …")
            sb.table("all_integration_api_keys").upsert(
                {
                    "organization_id": org_id,
                    "integration_slug": slug,
                    "encrypted_api_key": b64_ct,
                    "is_active": True,
                    "last_verified_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="organization_id,integration_slug",
                ignore_duplicates=False,
            ).execute()
            _vlog("✅ Integration key saved")
        else:
            _vlog("💾 Saving product token to smartsheet_tokens …")
            sb.table("smartsheet_tokens").upsert(
                {
                    "organization_id": org_id,
                    "product_slug": slug,
                    "encrypted_token": b64_ct,
                    "verified_key_email": verified_email if verified_email and verified_email != "—" else None,
                    "verified_key_ss_region": ss_region,
                },
                on_conflict="organization_id,product_slug",
                ignore_duplicates=False,
            ).execute()
            _vlog("✅ Product token saved")

        # ── 6. Return success + verification metadata ─────────────
        audit_log.record_encrypt_event(
            sb,
            request_id=rid,
            event_type="encrypt_complete",
            outcome="success",
            http_status=200,
            organization_id=str(org_id),
            key_kind=key_kind,
            slug=slug,
            actor_user_id=getattr(g, "actor_user_id", None),
            meta={"ss_region": ss_region},
        )
        return jsonify(
            status="ok",
            encrypted_token=b64_ct,
            verified=True,
            request_id=rid,
            verification_data={
                "current_user": verification_result["current_user"],
                "users_count": len(verification_result.get("users", [])),
                "sheets_count": verification_result.get("sheets_count", 0),
                "region": ss_region,
                "base_url": base_url,
            },
        )

    except Exception as e:
        audit_log.record_encrypt_event(
            sb,
            request_id=rid,
            event_type="encrypt_error",
            outcome="server_error",
            http_status=500,
            organization_id=str(org_id) if org_id else None,
            key_kind=key_kind,
            slug=slug,
            actor_user_id=getattr(g, "actor_user_id", None),
            error_code=type(e).__name__,
            meta={"safe_message": str(e)[:200] if not _IS_PROD else None},
        )
        print(f"❌ Error during encryption/save: {e}")
        msg = "Encryption failed. Please try again or contact support." if _IS_PROD else f"Encryption failed: {str(e)}"
        return jsonify(error=msg, request_id=rid), 500


@app.route("/health")
def health_check():
    """ALB target group + direct probes: GET, no auth, minimal work (200 JSON)."""
    return jsonify(status="ok", service="encrypt-tool"), 200


@app.route("/encrypt/health")
def health_check_under_encrypt_prefix():
    """Same as /health when the public URL is …/encrypt (hub REACT_APP_ENCRYPT_URL + /health)."""
    return health_check()


@app.route("/")
def health():
    return "✅ encrypt-tool ready"


if __name__ == "__main__":
    PORT = 3051
    print(f"🔐  Encrypt tool listening on http://localhost:{PORT}")
    app.run("0.0.0.0", PORT, threaded=True)
