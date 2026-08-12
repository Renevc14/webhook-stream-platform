"""
Smartsheet Client Service
=========================

SH-102: the legacy per-process ``SmartsheetService`` class +
``get_smartsheet_service`` / ``get_smartsheet_for_organization`` /
``get_smartsheet_for_user`` / ``clear_client_cache`` helpers are gone.
All Smartsheet API access in i2c-3 now goes through
:func:`get_smartsheet_client`, which returns the async
``smarthub_core.platforms.smartsheet.SmartsheetClient`` (encrypted token
cache + cluster-wide rate limit + ``errorCode 4000`` / HTTP/2 GOAWAY
retry coverage).

What stays in this module:

  - :class:`SmartsheetError` — generic exception used by the new factory
    too.
  - :func:`get_token_and_base` — decrypts the per-org Smartsheet token
    from Postgres; still consumed by ``api/routes/smartsheet.py`` for
    one-off operations that do not need the full async client.
  - :func:`get_org_for_user` — resolves a Supabase user → org for the
    same route module.
  - :func:`get_smartsheet_client` — the SH-93 factory that wires the
    new ``SmartsheetClient`` against the shared Redis + Supabase
    singletons.
"""

import logging
from typing import Optional, Tuple, TYPE_CHECKING

from src.config.settings import get_settings
from src.config.constants import TABLE_NAMES
from smarthub_core.crypto import decrypt_token
from src.services.supabase.client import get_supabase_service
from src.services.smartsheet.smartsheet_broker import (
    NoSmartsheetCredentials,
    broker_configured,
    resolve_smartsheet_credentials,
)

if TYPE_CHECKING:
    from smarthub_core.platforms.smartsheet import SmartsheetClient


log = logging.getLogger(__name__)


class SmartsheetError(Exception):
    """Raised when Smartsheet operations fail."""
    pass



def get_token_and_base(organization_id: str, user_id: Optional[str] = None) -> Tuple[str, str]:
    """
    Get decrypted token and base URL for an organization.

    Broker-first: when the encrypt-tool credentials service is configured,
    resolve the calling user's per-tool OAuth token (US/EU) — the broker
    falls back to the org PAT server-side. Legacy smartsheet_tokens decrypt
    remains as the fallback for GOV/AUS and un-migrated environments.

    Args:
        organization_id: Organization UUID
        user_id: Supabase user whose OAuth token should be preferred

    Returns:
        Tuple of (token, base_url)

    Raises:
        SmartsheetError: If token not found
    """
    db = get_supabase_service()
    settings = get_settings()

    if broker_configured():
        try:
            brokered = resolve_smartsheet_credentials(
                organization_id, settings.product_slug, user_id
            )
        except NoSmartsheetCredentials as e:
            raise SmartsheetError(str(e)) from e
        if brokered:
            return brokered

    # Get encrypted token
    token_result = db.execute_with_retry(
        lambda: db.table(TABLE_NAMES.TOKENS)
            .select("encrypted_token")
            .eq("organization_id", organization_id)
            .eq("product_slug", settings.product_slug)
            .single()
            .execute(),
        f"get token for org {organization_id}"
    )

    if not token_result.data:
        raise SmartsheetError(f"No token found for organization {organization_id}")

    encrypted_token = token_result.data["encrypted_token"]
    token = decrypt_token(encrypted_token)

    # Get base URL
    base_result = db.execute_with_retry(
        lambda: db.table(TABLE_NAMES.ORG_BASE)
            .select("base_url")
            .eq("organization_id", organization_id)
            .single()
            .execute(),
        f"get base URL for org {organization_id}"
    )

    if not base_result.data or not base_result.data.get("base_url"):
        raise SmartsheetError(
            f"organization_ss_api_base has no base_url for org {organization_id}. "
            "Verify the organization's ss_region is set and exists in smartsheet_regions."
        )
    base_url = base_result.data["base_url"]

    return token, base_url


def get_smartsheet_client(
    organization_id: str,
    owner_user_id: Optional[str] = None,
) -> "SmartsheetClient":
    """Return a ``SmartsheetClient`` scoped to this org + the i2c-3 slug.

    ``owner_user_id`` (usually the config's ``created_by``) makes the token
    resolver prefer that user's per-tool OAuth token via the encrypt-tool
    broker on US/EU plans; PAT resolution is unchanged when it is omitted.

    Cheap to construct (state is lazily resolved on first call). Each call
    returns a fresh instance — the cached state lives in Redis under
    stable per-(org, slug) keys, so two instances for the same org share
    the same cache and rate-limit window.

    Raises:
        SmartsheetError: when Redis is not wired (lifespan failed to
            install a client). With the SH-102 cleanup the only fallback
            is to fix Redis — the legacy ``SmartsheetService`` path no
            longer exists.
    """
    from smarthub_core.platforms.smartsheet import SmartsheetClient
    from src.services.redis_client import get_redis_client

    redis = get_redis_client()
    if redis is None:
        raise SmartsheetError(
            "SmartsheetClient requires Redis but no RedisClient is "
            "installed. Set REDIS_URL and restart — there is no legacy "
            "fallback after SH-102."
        )

    settings = get_settings()
    return SmartsheetClient(
        org_id=organization_id,
        product_slug=settings.product_slug,
        redis=redis,
        supabase=get_supabase_service(),
        rate_limit_max_calls=settings.smartsheet_max_calls_per_minute,
        owner_user_id=owner_user_id,
    )


def get_org_for_user(user_id: str) -> str:
    """
    Get organization ID for a user.

    First tries to get the user's active plan organization,
    then falls back to user_profiles table.

    Args:
        user_id: Supabase user ID

    Returns:
        Organization ID

    Raises:
        SmartsheetError: If user/org not found
    """
    db = get_supabase_service()

    # First try to get the user's active plan organization
    # This ensures we use the correct organization when users switch plans
    try:
        result = db.client.rpc(
            'get_user_active_plan',
            {'p_user_id': user_id}
        ).execute()

        if result.data and len(result.data) > 0:
            org_id = result.data[0].get('organization_id')
            if org_id:
                return org_id
    except Exception as e:
        log.debug(f"RPC get_user_active_plan failed: {e}, trying fallback")

    # Fallback to user_profiles table
    try:
        result = db.execute_with_retry(
            lambda: db.table("user_profiles")
                .select("organization_id")
                .eq("id", user_id)
                .single()
                .execute(),
            f"get org for user {user_id}"
        )

        if result.data and result.data.get("organization_id"):
            return result.data["organization_id"]
    except Exception as e:
        log.debug(f"user_profiles lookup failed: {e}")

    raise SmartsheetError(f"No organization found for user {user_id}")


# SH-102: `get_smartsheet_for_user`, `clear_client_cache`,
# `get_smartsheet_service` and the legacy `SmartsheetService` class are
# all gone. The new `get_smartsheet_client` is org-scoped; callers that
# need a user → org lookup should call `get_org_for_user(user_id)` first.
