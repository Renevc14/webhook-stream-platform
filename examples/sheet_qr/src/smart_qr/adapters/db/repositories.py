from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import asyncpg

from smart_qr.adapters.db.pool import get_pool
from smart_qr.adapters.platform.memberships import get_membership_reader

log = logging.getLogger(__name__)

# Sentinel: distinguishes "not passed" from an explicit None (no platform reader).
_UNSET = object()


@dataclass
class ConfigRecord:
    id: UUID
    organization_id: UUID
    worksheet_id: int
    name: str
    content_mode: str
    form_base_url: Optional[str]
    content_column_ids: list[int]
    content_column_titles: list[str]
    template: Optional[str]
    form_label_overrides: dict[str, str]
    watched_column_ids: list[int]
    target_column_id: int
    write_mode: str
    code_type: str
    qr_ecc: str
    qr_box_size: int
    qr_border: int
    pause: bool
    runner_user_id: Optional[UUID]
    last_errors: list[Any]
    created_at: datetime
    updated_at: datetime
    has_logo: bool = False
    logo_filename: Optional[str] = None
    timestamp_column_id: Optional[int] = None
    timezone: str = "UTC"
    target_column_title: Optional[str] = None
    worksheet_name: Optional[str] = None
    qr_fill_color: str = "#000000"
    qr_back_color: str = "#FFFFFF"
    qr_logo_scale: float = 0.22
    attach_to_row: bool = False


class TargetColumnConflictError(RuntimeError):
    """A concurrent write tried to reuse a sheet's target image column."""


def _row_to_config(row: Any) -> ConfigRecord:
    overrides = row["form_label_overrides"]
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    errors = row["last_errors"]
    if isinstance(errors, str):
        errors = json.loads(errors)
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    if "has_logo" in keys:
        has_logo = bool(row["has_logo"])
    elif "logo_png" in keys:
        has_logo = row["logo_png"] is not None
    else:
        has_logo = False
    filename = row["logo_filename"] if "logo_filename" in keys else None
    return ConfigRecord(
        id=row["id"],
        organization_id=row["organization_id"],
        worksheet_id=int(row["worksheet_id"]),
        name=row["name"],
        content_mode=row["content_mode"],
        form_base_url=row["form_base_url"],
        content_column_ids=[int(x) for x in (row["content_column_ids"] or [])],
        content_column_titles=list(row["content_column_titles"] or []),
        template=row["template"],
        form_label_overrides={str(k): str(v) for k, v in (overrides or {}).items()},
        watched_column_ids=[int(x) for x in (row["watched_column_ids"] or [])],
        target_column_id=int(row["target_column_id"]),
        write_mode=row["write_mode"],
        code_type=row["code_type"],
        qr_ecc=row["qr_ecc"],
        qr_box_size=int(row["qr_box_size"]),
        qr_border=int(row["qr_border"]),
        pause=bool(row["pause"]),
        runner_user_id=row["runner_user_id"],
        last_errors=list(errors or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_logo=has_logo,
        logo_filename=filename,
        timestamp_column_id=(
            int(row["timestamp_column_id"])
            if "timestamp_column_id" in keys and row["timestamp_column_id"] is not None
            else None
        ),
        timezone=(
            str(row["timezone"]) if "timezone" in keys and row["timezone"] else "UTC"
        ),
        target_column_title=(
            str(row["target_column_title"])
            if "target_column_title" in keys and row["target_column_title"]
            else None
        ),
        worksheet_name=(
            str(row["worksheet_name"])
            if "worksheet_name" in keys and row["worksheet_name"]
            else None
        ),
        qr_fill_color=(
            str(row["qr_fill_color"])
            if "qr_fill_color" in keys and row["qr_fill_color"]
            else "#000000"
        ),
        qr_back_color=(
            str(row["qr_back_color"])
            if "qr_back_color" in keys and row["qr_back_color"]
            else "#FFFFFF"
        ),
        qr_logo_scale=(
            float(row["qr_logo_scale"])
            if "qr_logo_scale" in keys and row["qr_logo_scale"] is not None
            else 0.22
        ),
        attach_to_row=(
            bool(row["attach_to_row"]) if "attach_to_row" in keys else False
        ),
    )


_CONFIG_SELECT = """
    id, organization_id, worksheet_id, name, content_mode, form_base_url,
    content_column_ids, content_column_titles, template, form_label_overrides,
    watched_column_ids, target_column_id, write_mode, code_type, qr_ecc,
    qr_box_size, qr_border, pause, runner_user_id, last_errors, created_at, updated_at,
    logo_filename, (logo_png IS NOT NULL) AS has_logo,
    timestamp_column_id, timezone, target_column_title, worksheet_name,
    qr_fill_color, qr_back_color, qr_logo_scale, attach_to_row
"""


class ConfigRepository:
    async def list_for_org(self, organization_id: UUID) -> list[ConfigRecord]:
        pool = get_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_CONFIG_SELECT}
            FROM smartqr_configs
            WHERE organization_id=$1
            ORDER BY created_at DESC
            """,
            organization_id,
        )
        return [_row_to_config(r) for r in rows]

    async def get(
        self, config_id: UUID, organization_id: UUID
    ) -> Optional[ConfigRecord]:
        pool = get_pool()
        row = await pool.fetchrow(
            f"""
            SELECT {_CONFIG_SELECT}
            FROM smartqr_configs
            WHERE id=$1 AND organization_id=$2
            """,
            config_id,
            organization_id,
        )
        return _row_to_config(row) if row else None

    async def count_for_worksheet(
        self, organization_id: UUID, worksheet_id: int
    ) -> int:
        pool = get_pool()
        total = await pool.fetchval(
            """
            SELECT COUNT(*) FROM smartqr_configs
            WHERE organization_id=$1 AND worksheet_id=$2
            """,
            organization_id,
            worksheet_id,
        )
        return int(total or 0)

    async def list_for_worksheet(
        self, organization_id: UUID, worksheet_id: int
    ) -> list[ConfigRecord]:
        pool = get_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_CONFIG_SELECT}
            FROM smartqr_configs
            WHERE organization_id=$1 AND worksheet_id=$2 AND pause=FALSE
            """,
            organization_id,
            worksheet_id,
        )
        return [_row_to_config(r) for r in rows]

    async def find_target_conflict(
        self,
        organization_id: UUID,
        worksheet_id: int,
        target_column_id: int,
        *,
        exclude_config_id: Optional[UUID] = None,
    ) -> Optional[ConfigRecord]:
        """Another config already writing images into the same sheet column."""
        pool = get_pool()
        if exclude_config_id is not None:
            row = await pool.fetchrow(
                f"""
                SELECT {_CONFIG_SELECT}
                FROM smartqr_configs
                WHERE organization_id=$1
                  AND worksheet_id=$2
                  AND target_column_id=$3
                  AND id<>$4
                ORDER BY created_at ASC
                LIMIT 1
                """,
                organization_id,
                worksheet_id,
                target_column_id,
                exclude_config_id,
            )
        else:
            row = await pool.fetchrow(
                f"""
                SELECT {_CONFIG_SELECT}
                FROM smartqr_configs
                WHERE organization_id=$1
                  AND worksheet_id=$2
                  AND target_column_id=$3
                ORDER BY created_at ASC
                LIMIT 1
                """,
                organization_id,
                worksheet_id,
                target_column_id,
            )
        return _row_to_config(row) if row else None

    async def create(self, organization_id: UUID, data: dict[str, Any]) -> ConfigRecord:
        pool = get_pool()
        content_ids = data["content_column_ids"]
        watched = data.get("watched_column_ids") or content_ids
        try:
            row = await pool.fetchrow(
                """
                INSERT INTO smartqr_configs (
                    organization_id, worksheet_id, worksheet_name, name, content_mode, form_base_url,
                    content_column_ids, content_column_titles, template, form_label_overrides,
                    watched_column_ids, target_column_id, target_column_title, write_mode, code_type,
                    qr_ecc, qr_box_size, qr_border, pause, runner_user_id,
                    qr_fill_color, qr_back_color, qr_logo_scale, attach_to_row
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                    $21,$22,$23,$24
                )
                RETURNING id
                """,
                organization_id,
                data["worksheet_id"],
                data.get("worksheet_name"),
                data.get("name") or "SmartQR config",
                data.get("content_mode") or "identify",
                data.get("form_base_url"),
                content_ids,
                data.get("content_column_titles") or [],
                data.get("template"),
                json.dumps(data.get("form_label_overrides") or {}),
                watched,
                data["target_column_id"],
                data.get("target_column_title"),
                data.get("write_mode") or "replace",
                data.get("code_type") or "qr",
                "H",
                data.get("qr_box_size") or 4,
                data.get("qr_border") or 2,
                bool(data.get("pause", False)),
                data.get("runner_user_id"),
                data.get("qr_fill_color") or "#000000",
                data.get("qr_back_color") or "#FFFFFF",
                float(
                    data.get("qr_logo_scale")
                    if data.get("qr_logo_scale") is not None
                    else 0.22
                ),
                bool(data.get("attach_to_row", False)),
            )
        except asyncpg.UniqueViolationError as exc:
            if getattr(exc, "constraint_name", "") == (
                "uq_smartqr_configs_org_sheet_target"
            ):
                raise TargetColumnConflictError(
                    "Target image column is already used by another configuration"
                ) from exc
            raise
        log.info(
            "Created smartqr config id=%s org=%s worksheet=%s",
            row["id"],
            organization_id,
            data["worksheet_id"],
        )
        created = await self.get(row["id"], organization_id)
        assert created is not None
        return created

    async def set_pause(
        self, config_id: UUID, organization_id: UUID, pause: bool
    ) -> Optional[ConfigRecord]:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            UPDATE smartqr_configs
            SET pause=$3, updated_at=NOW()
            WHERE id=$1 AND organization_id=$2
            RETURNING id
            """,
            config_id,
            organization_id,
            pause,
        )
        return await self.get(config_id, organization_id) if row else None

    async def update(
        self, config_id: UUID, organization_id: UUID, data: dict[str, Any]
    ) -> Optional[ConfigRecord]:
        """Partial update of editable config fields (worksheet_id is immutable)."""
        pool = get_pool()
        existing = await self.get(config_id, organization_id)
        if existing is None:
            return None

        name = data.get("name", existing.name)
        content_mode = data.get("content_mode", existing.content_mode)
        form_base_url = data.get("form_base_url", existing.form_base_url)
        content_ids = data.get("content_column_ids", existing.content_column_ids)
        content_titles = data.get(
            "content_column_titles", existing.content_column_titles
        )
        watched = data.get("watched_column_ids")
        if watched is None:
            watched = list(content_ids or [])
        target_id = data.get("target_column_id", existing.target_column_id)
        target_title = data.get("target_column_title", existing.target_column_title)
        worksheet_name = data.get("worksheet_name", existing.worksheet_name)
        write_mode = data.get("write_mode", existing.write_mode)
        code_type = data.get("code_type", existing.code_type)
        template = data.get("template", existing.template)
        overrides = data.get("form_label_overrides", existing.form_label_overrides)
        qr_box_size = data.get("qr_box_size", existing.qr_box_size)
        qr_border = data.get("qr_border", existing.qr_border)
        qr_fill_color = data.get("qr_fill_color", existing.qr_fill_color)
        qr_back_color = data.get("qr_back_color", existing.qr_back_color)
        qr_logo_scale = data.get("qr_logo_scale", existing.qr_logo_scale)
        attach_to_row = data.get("attach_to_row", existing.attach_to_row)

        try:
            row = await pool.fetchrow(
                """
                UPDATE smartqr_configs SET
                    name=$3,
                    content_mode=$4,
                    form_base_url=$5,
                    content_column_ids=$6,
                    content_column_titles=$7,
                    watched_column_ids=$8,
                    target_column_id=$9,
                    target_column_title=$10,
                    write_mode=$11,
                    code_type=$12,
                    template=$13,
                    form_label_overrides=$14::jsonb,
                    worksheet_name=$15,
                    qr_box_size=$16,
                    qr_border=$17,
                    qr_fill_color=$18,
                    qr_back_color=$19,
                    qr_logo_scale=$20,
                    attach_to_row=$21,
                    qr_ecc='H',
                    updated_at=NOW()
                WHERE id=$1 AND organization_id=$2
                RETURNING id
                """,
                config_id,
                organization_id,
                name,
                content_mode,
                form_base_url,
                content_ids,
                content_titles,
                watched,
                target_id,
                target_title,
                write_mode,
                code_type,
                template,
                json.dumps(overrides or {}),
                worksheet_name,
                qr_box_size,
                qr_border,
                qr_fill_color,
                qr_back_color,
                qr_logo_scale,
                bool(attach_to_row),
            )
        except asyncpg.UniqueViolationError as exc:
            if getattr(exc, "constraint_name", "") == (
                "uq_smartqr_configs_org_sheet_target"
            ):
                raise TargetColumnConflictError(
                    "Target image column is already used by another configuration"
                ) from exc
            raise
        if not row:
            return None
        log.info("Updated smartqr config id=%s org=%s", config_id, organization_id)
        return await self.get(config_id, organization_id)

    async def delete(self, config_id: UUID, organization_id: UUID) -> bool:
        pool = get_pool()
        result = await pool.execute(
            "DELETE FROM smartqr_configs WHERE id=$1 AND organization_id=$2",
            config_id,
            organization_id,
        )
        return result.endswith("1")

    async def set_logo(
        self,
        config_id: UUID,
        organization_id: UUID,
        *,
        png_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> Optional[ConfigRecord]:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            UPDATE smartqr_configs
            SET logo_png=$3, logo_filename=$4, logo_content_type=$5, updated_at=NOW()
            WHERE id=$1 AND organization_id=$2
            RETURNING id
            """,
            config_id,
            organization_id,
            png_bytes,
            filename,
            content_type,
        )
        return await self.get(config_id, organization_id) if row else None

    async def clear_logo(
        self, config_id: UUID, organization_id: UUID
    ) -> Optional[ConfigRecord]:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            UPDATE smartqr_configs
            SET logo_png=NULL, logo_filename=NULL, logo_content_type=NULL, updated_at=NOW()
            WHERE id=$1 AND organization_id=$2
            RETURNING id
            """,
            config_id,
            organization_id,
        )
        return await self.get(config_id, organization_id) if row else None

    async def get_logo_bytes(
        self, config_id: UUID, organization_id: UUID
    ) -> Optional[bytes]:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT logo_png FROM smartqr_configs
            WHERE id=$1 AND organization_id=$2
            """,
            config_id,
            organization_id,
        )
        if not row or row["logo_png"] is None:
            return None
        return bytes(row["logo_png"])


@dataclass(frozen=True)
class RowStateRecord:
    payload_hash: str
    attachment_id: Optional[int] = None
    attachment_payload_hash: Optional[str] = None


class RowStateRepository:
    async def get(self, config_id: UUID, row_id: int) -> Optional[RowStateRecord]:
        pool = get_pool()
        row = await pool.fetchrow(
            """
            SELECT payload_hash, attachment_id, attachment_payload_hash
            FROM smartqr_row_states
            WHERE config_id=$1 AND row_id=$2
            """,
            config_id,
            row_id,
        )
        if row is None:
            return None
        return RowStateRecord(
            payload_hash=str(row["payload_hash"]),
            attachment_id=(
                int(row["attachment_id"]) if row["attachment_id"] is not None else None
            ),
            attachment_payload_hash=(
                str(row["attachment_payload_hash"])
                if row["attachment_payload_hash"]
                else None
            ),
        )

    async def get_hash(self, config_id: UUID, row_id: int) -> Optional[str]:
        state = await self.get(config_id, row_id)
        return state.payload_hash if state else None

    async def upsert_hash(
        self, config_id: UUID, row_id: int, payload_hash: str
    ) -> None:
        pool = get_pool()
        await pool.execute(
            """
            INSERT INTO smartqr_row_states (config_id, row_id, payload_hash, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (config_id, row_id)
            DO UPDATE SET payload_hash=EXCLUDED.payload_hash, updated_at=NOW()
            """,
            config_id,
            row_id,
            payload_hash,
        )

    async def set_attachment(
        self,
        config_id: UUID,
        row_id: int,
        payload_hash: str,
        attachment_id: int,
    ) -> None:
        """Record only the row attachment created and owned by SmartQR."""
        pool = get_pool()
        await pool.execute(
            """
            UPDATE smartqr_row_states
            SET attachment_id=$3,
                attachment_payload_hash=$4,
                updated_at=NOW()
            WHERE config_id=$1 AND row_id=$2
            """,
            config_id,
            row_id,
            int(attachment_id),
            payload_hash,
        )

    # Marker written when style/logo changes force a regenerate. Must never equal
    # a real payload_hash (sha256 hex). Keeps attachment_id so the next write can
    # delete the SmartQR-owned file instead of orphaning it (SH-233).
    INVALIDATED_PAYLOAD_HASH = "invalidated"

    async def clear_for_config(self, config_id: UUID) -> int:
        """Invalidate row hashes after a style/logo change without losing ownership.

        Previously this DELETE'd every ``smartqr_row_states`` row, which also
        dropped ``attachment_id``. The next regenerate then uploaded a new
        printable attachment without deleting the old one, so sheets accumulated
        ``smartqr-*.png`` files. We only invalidate the hashes; ``attachment_id``
        stays so ``_replace_row_attachment`` can remove the prior file.
        """
        pool = get_pool()
        result = await pool.execute(
            """
            UPDATE smartqr_row_states
            SET payload_hash=$2,
                attachment_payload_hash=NULL,
                updated_at=NOW()
            WHERE config_id=$1
            """,
            config_id,
            self.INVALIDATED_PAYLOAD_HASH,
        )
        # asyncpg returns "UPDATE N"
        try:
            n = int(str(result).split()[-1])
        except (TypeError, ValueError, IndexError):
            n = 0
        if n:
            log.info(
                "Invalidated %s row hashes for config=%s (attachment_id preserved)",
                n,
                config_id,
            )
        return n


class OrgMembershipRepository:
    """Confirms the caller really belongs to the org they claim in the header.

    `user_organization_memberships` is the source of truth: the Hub lets a user
    switch active plan, and `user_profiles.organization_id` keeps pointing at
    their default org, so the profile alone would reject legitimate sessions.
    The profile is only a fallback for orgs that predate memberships.

    Both tables belong to the Hub and their RLS policies are written for browser
    sessions, so our Postgres role sees zero rows. When service role credentials
    are configured the check goes through the Supabase REST API (which bypasses
    RLS); the direct SQL path below is only for a standalone local database that
    has no platform tables at all.
    """

    _MEMBERSHIP_SQL = """
        SELECT 1 FROM user_organization_memberships
        WHERE user_id=$1::uuid AND organization_id=$2::uuid
        LIMIT 1
    """
    _PROFILE_SQL = "SELECT organization_id FROM user_profiles WHERE id=$1::uuid LIMIT 1"

    def __init__(self, platform_reader: Any = _UNSET) -> None:
        """`platform_reader=None` forces the direct SQL path (used by its tests)."""
        self._platform_reader = (
            get_membership_reader() if platform_reader is _UNSET else platform_reader
        )

    async def user_belongs_to_org(
        self, user_id: str, organization_id: UUID
    ) -> Optional[bool]:
        """True/False when it can be answered, None when the check is impossible.

        None means unknown (platform unreachable, or neither table present in a
        standalone database). Callers decide the policy for that case.
        """
        if self._platform_reader is not None:
            return await self._platform_reader.user_belongs_to_org(
                user_id, organization_id
            )
        return await self._user_belongs_to_org_via_sql(user_id, organization_id)

    async def default_org_for_user(self, user_id: str) -> Optional[UUID]:
        """The org on the user's profile, or None when it cannot be determined."""
        if self._platform_reader is not None:
            return await self._platform_reader.default_org_for_user(user_id)
        try:
            row = await get_pool().fetchrow(self._PROFILE_SQL, str(user_id))
        except (RuntimeError, asyncpg.PostgresError, OSError) as exc:
            log.error("Could not read the profile org for user=%s: %s", user_id, exc)
            return None
        if row is None or not row["organization_id"]:
            return None
        return UUID(str(row["organization_id"]))

    async def _user_belongs_to_org_via_sql(
        self, user_id: str, organization_id: UUID
    ) -> Optional[bool]:
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                pool = get_pool()
                memberships_available = True
                try:
                    row = await pool.fetchrow(
                        self._MEMBERSHIP_SQL, str(user_id), str(organization_id)
                    )
                    if row is not None:
                        return True
                except asyncpg.UndefinedTableError:
                    memberships_available = False

                profile = await pool.fetchrow(self._PROFILE_SQL, str(user_id))
                if profile is None:
                    log.warning(
                        "No user_profiles row for user=%s — refusing org=%s",
                        user_id,
                        organization_id,
                    )
                    return False
                if str(profile["organization_id"]) == str(organization_id):
                    return True
                log.warning(
                    "User %s is not a member of org=%s (memberships_table=%s)",
                    user_id,
                    organization_id,
                    memberships_available,
                )
                return False
            except asyncpg.UndefinedTableError:
                log.warning(
                    "Neither user_organization_memberships nor user_profiles present "
                    "— membership check unavailable"
                )
                return None
            except asyncpg.DataError:
                log.warning("Malformed user id for membership check user=%s", user_id)
                return False
            except (RuntimeError, asyncpg.PostgresError, OSError) as exc:
                last_err = exc
                if attempt < 2:
                    delay = 0.2 * (2**attempt)
                    log.warning(
                        "Membership check attempt %s failed (retry in %.1fs): %s",
                        attempt + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
        log.error(
            "Membership check failed for user=%s org=%s: %s",
            user_id,
            organization_id,
            last_err,
        )
        return None


class UsageRepository:
    """Owns `smartqr_usage_log` — the durable record the Hub bills from.

    `user_id` is the identity that generated the code, which is what the Hub's
    per-creator usage reports attribute the spend to.

    `operation_key` is the idempotency key for one billable generation
    (typically ``{config_id}:{row_id}:{payload_hash}``). Replays that hit the
    unique index return ``False`` so the caller can refund the Redis hold.
    """

    async def log_success(
        self,
        organization_id: UUID,
        config_id: UUID,
        row_id: int,
        code_type: str,
        bolts: int = 1,
        user_id: Optional[str] = None,
        *,
        operation_key: Optional[str] = None,
    ) -> bool:
        """Insert a success usage row. Returns True when a new charge was recorded."""
        pool = get_pool()
        if operation_key:
            row = await pool.fetchrow(
                """
                INSERT INTO smartqr_usage_log
                    (organization_id, user_id, config_id, row_id, code_type,
                     bolts, status, operation_key)
                VALUES ($1,$2::uuid,$3,$4,$5,$6,'success',$7)
                ON CONFLICT (organization_id, operation_key)
                    WHERE operation_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                organization_id,
                str(user_id) if user_id else None,
                config_id,
                row_id,
                code_type,
                bolts,
                operation_key,
            )
            return row is not None
        await pool.execute(
            """
            INSERT INTO smartqr_usage_log
                (organization_id, user_id, config_id, row_id, code_type, bolts, status)
            VALUES ($1,$2::uuid,$3,$4,$5,$6,'success')
            """,
            organization_id,
            str(user_id) if user_id else None,
            config_id,
            row_id,
            code_type,
            bolts,
        )
        return True


class WebhookRepository:
    """Owns smartqr_webhooks + platform_webhook_registrations (SH-230 D).

    Platform is write SoT (required). ``smartqr_webhooks`` is a soft-fail
    mirror for rollback until the tool table is retired.

    SH-215: no secret cache here. SmartQR does not receive callbacks, so it
    never verifies an HMAC; the gateway does, against a cache it alone owns and
    fills by decrypting ``shared_secret`` from the platform table (or the tool
    mirror while dual-write continues).
    """

    TOOL_SLUG = "smart-qr"
    PROVIDER = "smartsheet"

    async def upsert(
        self,
        *,
        organization_id: UUID,
        worksheet_id: int,
        webhook_id: int,
        shared_secret_encrypted: str,
        runner_user_id: Optional[UUID],
        callback_url: Optional[str] = None,
    ) -> None:
        from smarthub_core.webhooks.platform_registrations import (
            PLATFORM_UPSERT_SQL,
        )

        pool = get_pool()
        # Platform first (required).
        await pool.execute(
            PLATFORM_UPSERT_SQL,
            self.PROVIDER,
            self.TOOL_SLUG,
            organization_id,
            worksheet_id,
            webhook_id,
            shared_secret_encrypted or None,
            callback_url,
            "active",
            runner_user_id,
            None,  # config_id
        )

        # Tool-table soft-fail mirror.
        try:
            await pool.execute(
                """
                DELETE FROM smartqr_webhooks
                WHERE organization_id=$1 AND worksheet_id=$2
                """,
                organization_id,
                worksheet_id,
            )
            await pool.execute(
                """
                INSERT INTO smartqr_webhooks (
                    organization_id, worksheet_id, webhook_id, shared_secret_encrypted,
                    status, runner_user_id, updated_at
                ) VALUES ($1,$2,$3,$4,'active',$5,NOW())
                """,
                organization_id,
                worksheet_id,
                webhook_id,
                shared_secret_encrypted,
                runner_user_id,
            )
        except Exception as exc:
            log.warning(
                "smartqr_webhooks mirror soft-fail org=%s sheet=%s: %s",
                organization_id,
                worksheet_id,
                exc,
            )

    async def delete_for_worksheet(
        self, organization_id: UUID, worksheet_id: int
    ) -> Optional[int]:
        """Forget the webhook for a sheet. Returns the Smartsheet webhook id, if any."""
        from smarthub_core.webhooks.platform_registrations import (
            PLATFORM_DELETE_BY_WORKSHEET_SQL,
        )

        pool = get_pool()
        # Prefer webhook_id from tool mirror for Smartsheet-side cleanup return.
        row = await pool.fetchrow(
            """
            SELECT webhook_id FROM smartqr_webhooks
            WHERE organization_id=$1 AND worksheet_id=$2
            """,
            organization_id,
            worksheet_id,
        )
        webhook_id = int(row["webhook_id"]) if row and row["webhook_id"] else None

        # Platform delete by identity (required).
        await pool.execute(
            PLATFORM_DELETE_BY_WORKSHEET_SQL,
            self.PROVIDER,
            self.TOOL_SLUG,
            organization_id,
            worksheet_id,
        )

        try:
            await pool.execute(
                """
                DELETE FROM smartqr_webhooks
                WHERE organization_id=$1 AND worksheet_id=$2
                """,
                organization_id,
                worksheet_id,
            )
        except Exception as exc:
            log.warning(
                "smartqr_webhooks delete soft-fail org=%s sheet=%s: %s",
                organization_id,
                worksheet_id,
                exc,
            )
        return webhook_id
