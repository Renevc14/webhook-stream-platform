"""SH-178 part B — runner failover when OAuth is revoked (TokenRevokedError).

When the config's runner loses an authoritative Smartsheet connection (broker
409 / TokenRevokedError) and no transition PAT keeps work running, try other
active org members. The first candidate whose token resolves becomes the new
runner via ``ConfigRepository.set_runner``. If nobody qualifies, park the
config with ``mark_needs_connection``.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from src.services.smartsheet.client import get_smartsheet_client
from src.services.supabase.client import get_supabase_service
from src.services.supabase.config_repo import ConfigRepository

log = logging.getLogger(__name__)


def classify_credential_failure(exc: BaseException) -> Optional[str]:
    """Walk the cause chain to detect broker credential states (SH-182).

    Returns ``"reconnect_required"`` for an authoritative revoked/terminal
    OAuth state, ``"connection_missing"`` when no credentials exist, or
    ``None`` for ordinary upstream failures.
    """
    from smarthub_core.platforms.shared.token_resolver import (
        TokenNotFoundError,
        TokenRevokedError,
    )
    from src.services.smartsheet.smartsheet_broker import NoSmartsheetCredentials

    seen = 0
    cause: Optional[BaseException] = exc
    while cause is not None and seen < 10:
        if isinstance(cause, TokenRevokedError):
            return "reconnect_required"
        if isinstance(cause, (TokenNotFoundError, NoSmartsheetCredentials)):
            return "connection_missing"
        cause = cause.__cause__ or cause.__context__
        seen += 1
    return None


def list_active_org_member_ids(organization_id: str) -> List[str]:
    """Active membership user ids for the org (failover candidate pool)."""
    db = get_supabase_service()
    result = db.execute_with_retry(
        lambda: db.table("user_organization_memberships")
        .select("user_id")
        .eq("organization_id", organization_id)
        .eq("is_active", True)
        .execute(),
        f"list active members for org {organization_id}",
    )
    out: List[str] = []
    for row in result.data or []:
        uid = row.get("user_id")
        if uid:
            out.append(str(uid))
    return out


async def probe_runner_token(organization_id: str, user_id: str) -> bool:
    """True when the user's Smartsheet credentials resolve successfully."""
    try:
        client = get_smartsheet_client(organization_id, user_id)
        await client.get_client(force_refresh=True)
        return True
    except Exception as exc:
        state = classify_credential_failure(exc)
        log.info(
            "SH-178: probe failed org=%s user=%s state=%s err=%s",
            organization_id,
            user_id,
            state or "other",
            type(exc).__name__,
        )
        return False


async def find_failover_runner(
    organization_id: str,
    *,
    exclude_user_ids: Sequence[str] = (),
) -> Optional[str]:
    """Return the first org member (not excluded) whose token resolves."""
    excluded = {str(u) for u in exclude_user_ids if u}
    try:
        candidates = list_active_org_member_ids(organization_id)
    except Exception as exc:
        log.error(
            "SH-178: cannot list org members for failover org=%s: %s",
            organization_id,
            exc,
        )
        return None

    for uid in candidates:
        if uid in excluded:
            continue
        if await probe_runner_token(organization_id, uid):
            log.info(
                "SH-178: failover candidate accepted org=%s user=%s",
                organization_id,
                uid,
            )
            return uid
    return None


async def failover_configs(
    configs: Iterable[dict],
    *,
    organization_id: str,
    revoked_runner_user_id: Optional[str],
    config_repo: Optional[ConfigRepository] = None,
) -> dict:
    """Reassign or park configs after their runner's OAuth was revoked.

    Returns a summary dict: ``{reassigned, parked, new_runner}``.
    """
    repo = config_repo or ConfigRepository()
    config_list = [c for c in configs if c.get("id")]
    if not config_list:
        return {"reassigned": 0, "parked": 0, "new_runner": None}

    new_runner = await find_failover_runner(
        organization_id,
        exclude_user_ids=[revoked_runner_user_id] if revoked_runner_user_id else [],
    )

    reassigned = 0
    parked = 0
    if new_runner:
        for cfg in config_list:
            try:
                repo.set_runner(cfg["id"], new_runner)
                reassigned += 1
            except Exception as exc:
                log.error(
                    "SH-178: set_runner failed config=%s: %s — parking instead",
                    cfg.get("id"),
                    exc,
                )
                try:
                    repo.mark_needs_connection(cfg["id"])
                    parked += 1
                except Exception as park_exc:
                    log.error(
                        "SH-178: mark_needs_connection failed config=%s: %s",
                        cfg.get("id"),
                        park_exc,
                    )
    else:
        for cfg in config_list:
            try:
                repo.mark_needs_connection(cfg["id"])
                parked += 1
            except Exception as exc:
                log.error(
                    "SH-178: mark_needs_connection failed config=%s: %s",
                    cfg.get("id"),
                    exc,
                )

    log.warning(
        "SH-178: failover complete org=%s revoked_runner=%s "
        "reassigned=%d parked=%d new_runner=%s",
        organization_id,
        revoked_runner_user_id,
        reassigned,
        parked,
        new_runner,
    )
    return {
        "reassigned": reassigned,
        "parked": parked,
        "new_runner": new_runner,
    }
