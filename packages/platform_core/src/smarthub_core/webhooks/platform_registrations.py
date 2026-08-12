"""Platform webhook registration SoT helpers (SH-230 D).

``platform_webhook_registrations`` is the authoritative write target when the
gateway reads with ``PLATFORM_WEBHOOK_READ``. Per-tool ``*_webhooks`` tables
remain a soft-fail mirror for rollback until a follow-up retires them.

Identity key: ``(provider, tool_slug, organization_id, worksheet_id)``.
Secrets must already be ciphertext (``APP_CRYPTO_KEY`` AES-GCM) — never
plaintext.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

log = logging.getLogger(__name__)

PLATFORM_WEBHOOKS_TABLE = "platform_webhook_registrations"
DEFAULT_PROVIDER = "smartsheet"

# asyncpg / raw-SQL upsert for tools that do not use the Supabase client
# (SmartQR). Columns match the Supabase helper payload.
PLATFORM_UPSERT_SQL = """
INSERT INTO platform_webhook_registrations (
    provider, tool_slug, organization_id, worksheet_id, webhook_id,
    shared_secret, callback_url, status, runner_user_id, config_id,
    created_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    NOW(), NOW()
)
ON CONFLICT (provider, tool_slug, organization_id, worksheet_id)
DO UPDATE SET
    webhook_id = EXCLUDED.webhook_id,
    shared_secret = COALESCE(EXCLUDED.shared_secret, platform_webhook_registrations.shared_secret),
    callback_url = COALESCE(EXCLUDED.callback_url, platform_webhook_registrations.callback_url),
    status = EXCLUDED.status,
    runner_user_id = COALESCE(EXCLUDED.runner_user_id, platform_webhook_registrations.runner_user_id),
    config_id = COALESCE(EXCLUDED.config_id, platform_webhook_registrations.config_id),
    updated_at = NOW()
"""

PLATFORM_DELETE_BY_WEBHOOK_SQL = """
DELETE FROM platform_webhook_registrations
WHERE provider = $1 AND tool_slug = $2 AND webhook_id = $3
"""

PLATFORM_DELETE_BY_WORKSHEET_SQL = """
DELETE FROM platform_webhook_registrations
WHERE provider = $1 AND tool_slug = $2
  AND organization_id = $3 AND worksheet_id = $4
"""


class _SupabaseLike(Protocol):
    def table(self, name: str): ...

    def execute_with_retry(self, operation, description: str = "operation", **kwargs): ...


class PlatformRegistrationError(RuntimeError):
    """Raised when a required platform write fails."""


def upsert_platform_registration(
    supabase: _SupabaseLike,
    *,
    provider: str,
    tool_slug: str,
    organization_id: str,
    worksheet_id: int,
    webhook_id: int,
    shared_secret_ciphertext: Optional[str] = None,
    callback_url: Optional[str] = None,
    status: str = "active",
    runner_user_id: Optional[str] = None,
    config_id: Optional[str] = None,
    soft_fail: bool = False,
) -> bool:
    """Upsert one platform row.

    When ``soft_fail`` is False (default for tools / platform-as-SoT), failures
    raise ``PlatformRegistrationError``. Soft-fail returns False and logs —
    kept for transitional gateway dual-write call sites.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "provider": provider,
        "tool_slug": tool_slug,
        "organization_id": organization_id,
        "worksheet_id": int(worksheet_id),
        "webhook_id": int(webhook_id),
        "status": status,
        "updated_at": now,
    }
    if shared_secret_ciphertext is not None:
        payload["shared_secret"] = shared_secret_ciphertext
    if callback_url is not None:
        payload["callback_url"] = callback_url
    if runner_user_id is not None:
        payload["runner_user_id"] = runner_user_id
    if config_id is not None:
        payload["config_id"] = config_id

    try:
        existing = supabase.execute_with_retry(
            lambda: supabase.table(PLATFORM_WEBHOOKS_TABLE)
            .select("id")
            .eq("provider", provider)
            .eq("tool_slug", tool_slug)
            .eq("organization_id", organization_id)
            .eq("worksheet_id", int(worksheet_id))
            .execute(),
            f"find {PLATFORM_WEBHOOKS_TABLE}",
        )
        rows = list(existing.data or [])
        if rows:
            row_id = rows[0]["id"]
            supabase.execute_with_retry(
                lambda: supabase.table(PLATFORM_WEBHOOKS_TABLE)
                .update(payload)
                .eq("id", row_id)
                .execute(),
                f"update {PLATFORM_WEBHOOKS_TABLE}",
            )
        else:
            payload["created_at"] = now
            supabase.execute_with_retry(
                lambda: supabase.table(PLATFORM_WEBHOOKS_TABLE)
                .insert(payload)
                .execute(),
                f"insert {PLATFORM_WEBHOOKS_TABLE}",
            )
        return True
    except Exception as exc:
        log.warning(
            "platform_webhook_registrations upsert failed tool=%s org=%s "
            "sheet=%s webhook=%s soft_fail=%s: %s",
            tool_slug,
            organization_id,
            worksheet_id,
            webhook_id,
            soft_fail,
            exc,
        )
        if soft_fail:
            return False
        raise PlatformRegistrationError(
            f"platform upsert failed for {tool_slug}/{worksheet_id}: {exc}"
        ) from exc


def delete_platform_registration(
    supabase: _SupabaseLike,
    *,
    provider: str,
    tool_slug: str,
    webhook_id: int,
    soft_fail: bool = False,
) -> bool:
    """Delete platform rows for a Smartsheet webhook id."""
    try:
        supabase.execute_with_retry(
            lambda: supabase.table(PLATFORM_WEBHOOKS_TABLE)
            .delete()
            .eq("provider", provider)
            .eq("tool_slug", tool_slug)
            .eq("webhook_id", int(webhook_id))
            .execute(),
            f"delete {PLATFORM_WEBHOOKS_TABLE}",
        )
        return True
    except Exception as exc:
        log.warning(
            "platform_webhook_registrations delete failed tool=%s webhook=%s "
            "soft_fail=%s: %s",
            tool_slug,
            webhook_id,
            soft_fail,
            exc,
        )
        if soft_fail:
            return False
        raise PlatformRegistrationError(
            f"platform delete failed for {tool_slug}/{webhook_id}: {exc}"
        ) from exc


def delete_platform_registration_by_worksheet(
    supabase: _SupabaseLike,
    *,
    provider: str,
    tool_slug: str,
    organization_id: str,
    worksheet_id: int,
    soft_fail: bool = False,
) -> bool:
    """Delete platform row by identity key (org + worksheet)."""
    try:
        supabase.execute_with_retry(
            lambda: supabase.table(PLATFORM_WEBHOOKS_TABLE)
            .delete()
            .eq("provider", provider)
            .eq("tool_slug", tool_slug)
            .eq("organization_id", organization_id)
            .eq("worksheet_id", int(worksheet_id))
            .execute(),
            f"delete {PLATFORM_WEBHOOKS_TABLE} by worksheet",
        )
        return True
    except Exception as exc:
        log.warning(
            "platform_webhook_registrations delete-by-worksheet failed "
            "tool=%s org=%s sheet=%s soft_fail=%s: %s",
            tool_slug,
            organization_id,
            worksheet_id,
            soft_fail,
            exc,
        )
        if soft_fail:
            return False
        raise PlatformRegistrationError(
            f"platform delete-by-worksheet failed for {tool_slug}/{worksheet_id}: {exc}"
        ) from exc


def count_platform_vs_tool_parity(
    supabase: _SupabaseLike,
    *,
    provider: str,
    tool_slug: str,
    tool_table: str,
    status_value: str = "active",
) -> dict[str, int]:
    """Compare active webhook_id sets (tool table vs platform). For ops/parity."""

    def _ids(table: str, extra_eq: dict[str, Any]) -> set[int]:
        q = supabase.table(table).select("webhook_id").eq("status", status_value)
        for k, v in extra_eq.items():
            q = q.eq(k, v)
        result = supabase.execute_with_retry(
            lambda: q.execute(), f"parity list {table}"
        )
        out: set[int] = set()
        for row in list(result.data or []):
            wid = row.get("webhook_id")
            if wid is not None:
                out.add(int(wid))
        return out

    tool_ids = _ids(tool_table, {})
    platform_ids = _ids(
        PLATFORM_WEBHOOKS_TABLE,
        {"provider": provider, "tool_slug": tool_slug},
    )
    only_tool = tool_ids - platform_ids
    only_platform = platform_ids - tool_ids
    return {
        "tool_count": len(tool_ids),
        "platform_count": len(platform_ids),
        "only_tool": len(only_tool),
        "only_platform": len(only_platform),
        "matched": len(tool_ids & platform_ids),
    }
