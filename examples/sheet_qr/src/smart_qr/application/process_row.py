from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from smart_qr.adapters.billing.port import BillingPort
from smart_qr.adapters.db.repositories import (
    ConfigRecord,
    ConfigRepository,
    RowStateRepository,
    UsageRepository,
)
from smart_qr.adapters.smartsheet.client import SmartsheetPort, SmartsheetProvider
from smart_qr.adapters.smartsheet.credentials import (
    CredentialsMissing,
    CredentialsRevoked,
    CredentialsUnavailable,
)
from smart_qr.config.constants import (
    CHANGE_AGENT_ID,
    DEFAULT_QR_BACK,
    DEFAULT_QR_BORDER,
    DEFAULT_QR_BOX,
    DEFAULT_QR_ECC,
    DEFAULT_QR_FILL,
    DEFAULT_QR_LOGO_SCALE,
)
from smart_qr.domain.code_renderer import (
    RenderOptions,
    clamp_border,
    clamp_box_size,
    clamp_logo_scale,
    normalize_hex_color,
    render_png,
)
from smart_qr.domain.hashutil import payload_hash
from smart_qr.domain.payload_builder import (
    PayloadBuildError,
    PayloadSourceError,
    build_advanced_template,
    build_form_prefill_payload,
    build_identify_payload,
    build_open_row_payload,
)
from smart_qr.domain.write_policy import should_write

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    status: str
    reason: Optional[str] = None


class ProcessRowService:
    def __init__(
        self,
        *,
        configs: ConfigRepository,
        row_states: RowStateRepository,
        usage: UsageRepository,
        smartsheet: SmartsheetProvider,
        billing: BillingPort,
        change_agent: str = CHANGE_AGENT_ID,
    ) -> None:
        self._configs = configs
        self._row_states = row_states
        self._usage = usage
        self._smartsheet = smartsheet
        self._billing = billing
        self._change_agent = change_agent

    async def process(
        self,
        *,
        organization_id: UUID,
        config_id: UUID,
        row_id: int,
        changed_column_ids: Optional[list[int]] = None,
        change_agent: Optional[str] = None,
        force: bool = False,
        as_user_id: Optional[str] = None,
    ) -> ProcessResult:
        """Process one row. ``as_user_id`` overrides the config's runner.

        Interactive work (Generate / backfill) runs as the signed-in user so the
        write is attributed to whoever clicked; webhook and stream work runs as
        the config's stored runner, who is the only identity available then.
        """
        started = time.monotonic()

        def outcome(status: str, reason: Optional[str] = None) -> ProcessResult:
            """Every exit is logged: a silent skip is indistinguishable from a bug."""
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger = log.error if status == "error" else log.info
            logger(
                "SmartQR row %s org=%s config=%s row=%s reason=%s duration_ms=%s",
                status,
                organization_id,
                config_id,
                row_id,
                reason or "-",
                elapsed_ms,
            )
            return ProcessResult(status, reason)

        if change_agent and self._change_agent in change_agent:
            return outcome("skipped", "self_change_agent")

        cfg = await self._configs.get(config_id, organization_id)
        if cfg is None:
            return outcome("error", "config_not_found")
        if cfg.pause and not force:
            return outcome("skipped", "paused")

        if (
            not force
            and changed_column_ids is not None
            and cfg.watched_column_ids
            and not set(changed_column_ids) & set(cfg.watched_column_ids)
        ):
            return outcome("skipped", "watched_columns_unchanged")

        # Credentials belong to a person, not to the service: resolve them for the
        # identity that owns this write before touching Smartsheet.
        runner = as_user_id or (str(cfg.runner_user_id) if cfg.runner_user_id else None)
        try:
            ss = await self._smartsheet.get(organization_id, runner)
        except CredentialsRevoked as exc:
            return outcome("error", f"reconnect_required:{exc.oauth_state}")
        except CredentialsMissing:
            return outcome("error", "connection_missing")
        except CredentialsUnavailable as exc:
            return outcome("error", f"credentials_unavailable:{exc}")

        try:
            payload_text = await self._build_payload(cfg, row_id, ss)
        except PayloadSourceError as exc:
            return outcome("error", str(exc))
        except PayloadBuildError as exc:
            return outcome("skipped", f"payload:{exc}")

        code_type = cfg.code_type
        if (
            cfg.content_mode in ("open_row", "form_prefill", "advanced")
            and code_type != "qr"
        ):
            log.info(
                "%s forcing code_type=qr (was %s) config=%s",
                cfg.content_mode,
                code_type,
                config_id,
            )
            code_type = "qr"

        logo_bytes = await self._configs.get_logo_bytes(cfg.id, organization_id)
        logo_token = payload_hash(logo_bytes.hex() if logo_bytes else "nologo", "logo")
        fill = normalize_hex_color(
            getattr(cfg, "qr_fill_color", None) or DEFAULT_QR_FILL, DEFAULT_QR_FILL
        )
        back = normalize_hex_color(
            getattr(cfg, "qr_back_color", None) or DEFAULT_QR_BACK, DEFAULT_QR_BACK
        )
        box = clamp_box_size(getattr(cfg, "qr_box_size", None) or DEFAULT_QR_BOX)
        border = clamp_border(getattr(cfg, "qr_border", None) or DEFAULT_QR_BORDER)
        logo_scale = clamp_logo_scale(
            getattr(cfg, "qr_logo_scale", None)
            if getattr(cfg, "qr_logo_scale", None) is not None
            else DEFAULT_QR_LOGO_SCALE
        )
        style_token = f"{fill}|{back}|{box}|{border}|{logo_scale:.3f}"
        ph = payload_hash(f"{payload_text}|{logo_token}|{style_token}", code_type)
        state = await self._row_states.get(cfg.id, row_id)
        prev = state.payload_hash if state else None
        has_image = await ss.target_has_image(
            cfg.worksheet_id, row_id, cfg.target_column_id
        )
        cell_current = prev == ph and has_image
        attachment_current = False
        if (
            not force
            and cell_current
            and cfg.attach_to_row
            and state is not None
            and state.attachment_id is not None
            and state.attachment_payload_hash == ph
        ):
            attachment_current = await ss.row_attachment_exists(
                cfg.worksheet_id, row_id, state.attachment_id
            )

        # A current cell may still need its optional printable attachment.
        # That repair is not a new generation and must not reserve or charge.
        if not force and cell_current and (not cfg.attach_to_row or attachment_current):
            return outcome("skipped", "hash_unchanged")
        attachment_only = (
            not force and cell_current and cfg.attach_to_row and not attachment_current
        )
        if not attachment_only and not should_write(
            cfg.write_mode, has_image
        ):  # type: ignore[arg-type]
            return outcome("skipped", "fill_if_empty")

        png = render_png(
            payload_text,
            RenderOptions(
                code_type=code_type,  # type: ignore[arg-type]
                qr_ecc=DEFAULT_QR_ECC,
                qr_box_size=box,
                qr_border=border,
                logo_png=logo_bytes if code_type == "qr" else None,
                fill_color=fill,
                back_color=back,
                logo_scale=logo_scale,
            ),
        )
        if attachment_only:
            try:
                await self._replace_row_attachment(
                    cfg=cfg,
                    row_id=row_id,
                    png=png,
                    payload_hash_value=ph,
                    previous_attachment_id=state.attachment_id if state else None,
                    ss=ss,
                )
            except Exception as exc:  # noqa: BLE001
                return outcome("error", f"attachment_retry_failed:{exc}")
            return outcome(
                "ok",
                f"attached:{code_type}{'+logo' if logo_bytes else ''}:already_billed",
            )

        # operation_id ties the hold to this row attempt so release refunds the
        # exact bolts reserved even if pricing changes mid-flight.
        operation_id = f"{cfg.id}:{row_id}:{ph}"
        reservation = await self._billing.reserve(
            organization_id, 1, operation_id=operation_id
        )
        if not reservation.allowed:
            return outcome("skipped", f"bolt_denied:{reservation.reason or 'unknown'}")

        try:
            await ss.upload_cell_image(
                cfg.worksheet_id,
                row_id,
                cfg.target_column_id,
                png,
                self._change_agent,
            )
            # The usage row is the durable charge: the reservation above only
            # gates concurrency, the Hub invoices from this table.
            charged = await self._usage.log_success(
                organization_id,
                cfg.id,
                row_id,
                code_type,
                reservation.bolts,
                user_id=runner,
                operation_key=operation_id,
            )
            if not charged:
                # Same operation_key already billed (stream reclaim, or rewrite
                # after the destination image was cleared). Image/hash are
                # updated above; refund the Redis hold so we never double-spend.
                await self._billing.release(
                    organization_id,
                    bolts=reservation.bolts,
                    operation_id=operation_id,
                )
            # Persisting the hash after the durable charge makes a later
            # attachment-only retry provably already billed.
            await self._row_states.upsert_hash(cfg.id, row_id, ph)
        except Exception as exc:  # noqa: BLE001
            # Release the exact held amount — never reprice from current plan.
            await self._billing.release(
                organization_id,
                bolts=reservation.bolts,
                operation_id=operation_id,
            )
            return outcome("error", str(exc))

        if cfg.attach_to_row:
            try:
                await self._replace_row_attachment(
                    cfg=cfg,
                    row_id=row_id,
                    png=png,
                    payload_hash_value=ph,
                    previous_attachment_id=state.attachment_id if state else None,
                    ss=ss,
                )
            except Exception as exc:  # noqa: BLE001
                # The cell image was written and durably charged. Keep that
                # success state so the next attempt retries only the attachment.
                return outcome("error", f"attachment_failed_after_cell_write:{exc}")

        verb = "wrote" if charged else "rewrote"
        suffix = "" if charged else ":already_billed"
        return outcome(
            "ok",
            f"{verb}:{code_type}{'+logo' if logo_bytes else ''}"
            f"{'+attachment' if cfg.attach_to_row else ''}{suffix}",
        )

    async def _replace_row_attachment(
        self,
        *,
        cfg: ConfigRecord,
        row_id: int,
        png: bytes,
        payload_hash_value: str,
        previous_attachment_id: Optional[int],
        ss: SmartsheetPort,
    ) -> None:
        """Keep a single SmartQR printable attachment on the row.

        Deletes the tracked ``attachment_id`` (if still present) and any other
        row files that use our deterministic filename
        ``smartqr-{column_id}-{row_id}.png``. User-managed attachments with
        other names are left alone (SH-233).
        """
        filename = f"smartqr-{cfg.target_column_id}-{row_id}.png"
        refs = await ss.list_row_attachment_refs(cfg.worksheet_id, row_id)
        present = {aid for aid, _name in refs}
        to_delete: set[int] = set()
        if previous_attachment_id is not None:
            to_delete.add(int(previous_attachment_id))
        for aid, name in refs:
            if name == filename:
                to_delete.add(int(aid))
        for aid in sorted(to_delete):
            if aid not in present:
                continue
            await ss.delete_row_attachment(
                cfg.worksheet_id, aid, self._change_agent
            )
            log.info(
                "Removed prior SmartQR attachment sheet=%s row=%s attachment=%s",
                cfg.worksheet_id,
                row_id,
                aid,
            )
        attachment_id = await ss.upload_row_attachment(
            cfg.worksheet_id,
            row_id,
            png,
            self._change_agent,
            filename,
        )
        await self._row_states.set_attachment(
            cfg.id,
            row_id,
            payload_hash_value,
            attachment_id,
        )

    async def _build_payload(
        self, cfg: ConfigRecord, row_id: int, ss: SmartsheetPort
    ) -> str:
        # Never bill/write codes for blank Smartsheet rows (all modes).
        # Exclude the target image column so a prior QR does not count as "content".
        has_content = await ss.row_has_content(
            cfg.worksheet_id,
            row_id,
            exclude_column_ids=[int(cfg.target_column_id)],
        )
        if not has_content:
            raise PayloadBuildError("row has no non-empty content")

        if cfg.content_mode == "open_row":
            permalink = await ss.get_row_permalink(cfg.worksheet_id, row_id)
            return build_open_row_payload(permalink).text

        titles = list(cfg.content_column_titles)
        if not titles:
            titles = [str(i) for i in cfg.content_column_ids]
        reads = await ss.get_row_content_cells(cfg.worksheet_id, row_id, titles)
        # Cell images / attachments must never become ID or prefill text
        image_cols = [
            title for title, cell in reads.items() if getattr(cell, "has_image", False)
        ]
        if image_cols:
            cols = ", ".join(image_cols)
            raise PayloadSourceError(
                f"content_column_is_image:{cols} — "
                "ID to Code / Prefill Form / Custom link require text values, "
                "not cell images or attachments"
            )
        values = {
            title: (getattr(cell, "value", None) or "") for title, cell in reads.items()
        }

        if cfg.content_mode == "advanced":
            return build_advanced_template(cfg.template or "", values).text

        if cfg.content_mode == "form_prefill":
            mappings: list[tuple[str, str]] = []
            for title in titles:
                label = cfg.form_label_overrides.get(title, title)
                mappings.append((label, title))
            return build_form_prefill_payload(
                cfg.form_base_url or "", mappings, values
            ).text

        return build_identify_payload(titles, values).text
