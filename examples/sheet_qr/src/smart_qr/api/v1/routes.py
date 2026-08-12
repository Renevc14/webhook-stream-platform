from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import Response

from smart_qr.adapters.billing.port import get_billing
from smart_qr.adapters.db.repositories import (
    ConfigRepository,
    RowStateRepository,
    TargetColumnConflictError,
    UsageRepository,
)
from smart_qr.adapters.smartsheet.client import SmartsheetPort, get_smartsheet_provider
from smart_qr.adapters.smartsheet.credentials import (
    CredentialsMissing,
    CredentialsRevoked,
    CredentialsUnavailable,
)
from smart_qr.api.deps import AuthContext, require_auth
from smart_qr.api.v1.schemas import (
    BackfillRequest,
    ConfigCreate,
    ConfigOut,
    ConfigUpdate,
    EstimateBackfillRequest,
    GenerateRowRequest,
    InjectEventRequest,
)
from smart_qr.application.process_row import ProcessRowService
from smart_qr.application.webhook_lifecycle import (
    register_sheet_webhook,
    unregister_sheet_webhook,
)
from smart_qr.config.settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


async def _processor(settings: Settings) -> ProcessRowService:
    return ProcessRowService(
        configs=ConfigRepository(),
        row_states=RowStateRepository(),
        usage=UsageRepository(),
        smartsheet=get_smartsheet_provider(settings),
        billing=await get_billing(settings),
        change_agent=settings.change_agent_id,
    )


def _credentials_error(exc: CredentialsUnavailable) -> HTTPException:
    """Turn a credential problem into something the Hub can act on.

    409 means "the user has to do something" (connect or reconnect); 503 means
    "try again later". Both carry a stable `error_code` so the UI can show the
    Connect prompt instead of a generic failure.
    """
    if isinstance(exc, CredentialsRevoked):
        code, message = "SMARTSHEET_RECONNECT_REQUIRED", str(exc)
    elif isinstance(exc, CredentialsMissing):
        code, message = "SMARTSHEET_NOT_CONNECTED", str(exc)
    else:
        return HTTPException(
            503,
            detail={
                "error_code": "SMARTSHEET_CREDENTIALS_UNAVAILABLE",
                "message": str(exc),
            },
        )
    return HTTPException(409, detail={"error_code": code, "message": message})


BUDGET_MESSAGES = {
    "plan_suspended": "This organization's plan is suspended. Contact your admin to reactivate it.",
    "bolts_pending_payment": "There is a pending bolt payment for this organization.",
    "hard_cap_reached": "This organization has reached its bolt hard cap for the current cycle.",
    "plan_cannot_use_tools": "This organization's plan does not currently allow running tools.",
    "bolt_ceiling_reached": "No bolts left in this cycle. Raise the hard cap or top up to continue.",
    "insufficient_bolts": "Not enough bolts left in this cycle to generate every code in this run.",
    "product_not_priced": "SmartQR has no active price in the platform's pricing list yet.",
    "no_subscription": "This organization has no active subscription.",
    "budget_unavailable": "The organization's bolt balance could not be read. Try again shortly.",
}


async def _assert_budget(auth: AuthContext, settings: Settings, codes: int) -> None:
    """Refuse before generating anything rather than part-way through a run."""
    reason = await (await get_billing(settings)).preflight(auth.organization_id, codes)
    if not reason:
        return
    log.warning(
        "Refusing %s code(s) for org=%s — %s", codes, auth.organization_id, reason
    )
    status_code = 503 if reason == "budget_unavailable" else 409
    raise HTTPException(
        status_code,
        detail={
            "error_code": reason,
            "message": BUDGET_MESSAGES.get(reason, "Bolt budget check failed."),
        },
    )


async def _smartsheet_for(auth: AuthContext, settings: Settings) -> SmartsheetPort:
    """A client acting as the signed-in user for interactive requests."""
    try:
        return await get_smartsheet_provider(settings).get(
            auth.organization_id, auth.user_id
        )
    except CredentialsUnavailable as exc:
        log.error(
            "Smartsheet credentials unavailable org=%s user=%s: %s",
            auth.organization_id,
            auth.user_id,
            exc,
        )
        raise _credentials_error(exc) from exc


@router.get("/configs", response_model=list[ConfigOut])
async def list_configs(auth: AuthContext = Depends(require_auth)) -> list[ConfigOut]:
    rows = await ConfigRepository().list_for_org(auth.organization_id)
    return [ConfigOut.from_record(r) for r in rows]


def _normalize_config_payload(data: dict) -> dict:
    """Apply mode rules: QR-only modes, identify = single content column."""
    from smart_qr.domain.code_renderer import (
        RenderError,
        clamp_border,
        clamp_box_size,
        clamp_logo_scale,
        normalize_hex_color,
    )
    from smart_qr.config.constants import (
        DEFAULT_QR_BACK,
        DEFAULT_QR_BORDER,
        DEFAULT_QR_BOX,
        DEFAULT_QR_FILL,
        DEFAULT_QR_LOGO_SCALE,
    )

    mode = data.get("content_mode") or "identify"
    if mode not in ("identify", "form_prefill", "open_row", "advanced"):
        raise HTTPException(
            400,
            detail=(
                "content_mode must be one of: identify, form_prefill, "
                "open_row, advanced"
            ),
        )
    if mode == "open_row":
        data["content_column_ids"] = data.get("content_column_ids") or []
        data["content_column_titles"] = data.get("content_column_titles") or []
        data["watched_column_ids"] = data.get("watched_column_ids") or []
        data["form_base_url"] = None
        data["code_type"] = "qr"
    elif mode == "form_prefill":
        data["code_type"] = "qr"
    elif mode == "advanced":
        # Custom link: QR only; template holds placeholders, form_base_url unused.
        data["code_type"] = "qr"
        data["form_base_url"] = None
        if not (data.get("template") or "").strip():
            raise HTTPException(
                400, detail="template required for Custom link (advanced)"
            )
        data["template"] = (data.get("template") or "").strip()
    elif mode == "identify":
        ids = list(data.get("content_column_ids") or [])
        titles = list(data.get("content_column_titles") or [])
        if len(ids) > 1 or len(titles) > 1:
            raise HTTPException(
                400,
                detail="ID to Code (identify) allows only one content column",
            )
        if ids:
            data["content_column_ids"] = ids[:1]
        if titles:
            data["content_column_titles"] = titles[:1]
        if data.get("watched_column_ids"):
            data["watched_column_ids"] = list(data["watched_column_ids"])[:1]
        elif ids:
            data["watched_column_ids"] = ids[:1]
    data["qr_ecc"] = "H"
    try:
        data["qr_fill_color"] = normalize_hex_color(
            data.get("qr_fill_color"), DEFAULT_QR_FILL
        )
        data["qr_back_color"] = normalize_hex_color(
            data.get("qr_back_color"), DEFAULT_QR_BACK
        )
        data["qr_box_size"] = clamp_box_size(
            data.get("qr_box_size")
            if data.get("qr_box_size") is not None
            else DEFAULT_QR_BOX
        )
        data["qr_border"] = clamp_border(
            data.get("qr_border")
            if data.get("qr_border") is not None
            else DEFAULT_QR_BORDER
        )
        data["qr_logo_scale"] = clamp_logo_scale(
            data.get("qr_logo_scale")
            if data.get("qr_logo_scale") is not None
            else DEFAULT_QR_LOGO_SCALE
        )
    except RenderError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return data


@router.post("/configs", response_model=ConfigOut, status_code=status.HTTP_201_CREATED)
async def create_config(
    body: ConfigCreate,
    auth: AuthContext = Depends(require_auth),
) -> ConfigOut:
    if (
        body.content_mode != "open_row"
        and not body.content_column_ids
        and not body.content_column_titles
    ):
        raise HTTPException(
            400, detail="content_column_ids or content_column_titles required"
        )
    data = _normalize_config_payload(body.model_dump())
    settings = get_settings()
    # Resolve titles → column IDs when live token is set (webhook watch filter needs real IDs)
    if (
        data.get("content_mode") != "open_row"
        and not data.get("content_column_ids")
        and data.get("content_column_titles")
    ):
        try:
            resolved = await _resolve_column_ids(
                settings,
                auth,
                int(data["worksheet_id"]),
                list(data["content_column_titles"]),
            )
            if resolved:
                data["content_column_ids"] = resolved
                log.info(
                    "Resolved content_column_ids from titles sheet=%s ids=%s",
                    data["worksheet_id"],
                    resolved,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Column ID resolve skipped: %s", exc)
    if data.get("watched_column_ids") is None:
        data["watched_column_ids"] = list(data.get("content_column_ids") or [])
    if data.get("runner_user_id") is None:
        try:
            data["runner_user_id"] = UUID(auth.user_id)
        except ValueError:
            data["runner_user_id"] = None
    data["pause"] = False
    conflict = await ConfigRepository().find_target_conflict(
        auth.organization_id,
        int(data["worksheet_id"]),
        int(data["target_column_id"]),
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Target image column is already used by configuration “{conflict.name}”. "
                "Edit that configuration or choose a different target column."
            ),
        )
    try:
        rec = await ConfigRepository().create(auth.organization_id, data)
    except TargetColumnConflictError as exc:
        # The preflight above gives the friendly config name. This catches the
        # narrower concurrent-create race closed by the database unique index.
        raise HTTPException(
            status_code=409,
            detail=(
                "Target image column was claimed by another configuration. "
                "Refresh and choose a different target column."
            ),
        ) from exc
    return ConfigOut.from_record(rec)


@router.patch("/configs/{config_id}", response_model=ConfigOut)
async def update_config(
    config_id: UUID,
    body: ConfigUpdate,
    auth: AuthContext = Depends(require_auth),
) -> ConfigOut:
    existing = await ConfigRepository().get(config_id, auth.organization_id)
    if not existing:
        raise HTTPException(404, detail="Config not found")
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    merged = {
        "name": patch.get("name", existing.name),
        "content_mode": patch.get("content_mode", existing.content_mode),
        "form_base_url": patch.get("form_base_url", existing.form_base_url),
        "content_column_ids": patch.get(
            "content_column_ids", existing.content_column_ids
        ),
        "content_column_titles": patch.get(
            "content_column_titles", existing.content_column_titles
        ),
        "watched_column_ids": patch.get("watched_column_ids"),
        "target_column_id": patch.get("target_column_id", existing.target_column_id),
        "target_column_title": patch.get(
            "target_column_title", existing.target_column_title
        ),
        "worksheet_name": patch.get(
            "worksheet_name", getattr(existing, "worksheet_name", None)
        ),
        "write_mode": patch.get("write_mode", existing.write_mode),
        "code_type": patch.get("code_type", existing.code_type),
        "template": patch.get("template", existing.template),
        "form_label_overrides": patch.get(
            "form_label_overrides", existing.form_label_overrides
        ),
        "qr_box_size": patch.get("qr_box_size", existing.qr_box_size),
        "qr_border": patch.get("qr_border", existing.qr_border),
        "qr_fill_color": patch.get(
            "qr_fill_color", getattr(existing, "qr_fill_color", "#000000")
        ),
        "qr_back_color": patch.get(
            "qr_back_color", getattr(existing, "qr_back_color", "#FFFFFF")
        ),
        "qr_logo_scale": patch.get(
            "qr_logo_scale", getattr(existing, "qr_logo_scale", 0.22)
        ),
        "attach_to_row": patch.get(
            "attach_to_row", getattr(existing, "attach_to_row", False)
        ),
    }
    mode = merged["content_mode"]
    if (
        mode != "open_row"
        and not merged["content_column_ids"]
        and not merged["content_column_titles"]
    ):
        raise HTTPException(
            400, detail="content_column_ids or content_column_titles required"
        )
    if mode == "form_prefill" and not (merged.get("form_base_url") or "").strip():
        raise HTTPException(400, detail="form_base_url required for Prefill Form")
    if mode == "advanced" and not (merged.get("template") or "").strip():
        raise HTTPException(
            400, detail="template required for Custom link (advanced)"
        )
    merged = _normalize_config_payload(merged)
    if merged.get("watched_column_ids") is None:
        merged["watched_column_ids"] = list(merged.get("content_column_ids") or [])
    conflict = await ConfigRepository().find_target_conflict(
        auth.organization_id,
        int(existing.worksheet_id),
        int(merged["target_column_id"]),
        exclude_config_id=config_id,
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Target image column is already used by configuration “{conflict.name}”. "
                "Edit that configuration or choose a different target column."
            ),
        )
    try:
        rec = await ConfigRepository().update(config_id, auth.organization_id, merged)
    except TargetColumnConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Target image column was claimed by another configuration. "
                "Refresh and choose a different target column."
            ),
        ) from exc
    if not rec:
        raise HTTPException(404, detail="Config not found")
    style_changed = any(
        merged.get(k) != getattr(existing, k, None)
        for k in (
            "qr_box_size",
            "qr_border",
            "qr_fill_color",
            "qr_back_color",
            "qr_logo_scale",
            "code_type",
        )
    )
    if style_changed:
        invalidated = await RowStateRepository().clear_for_config(config_id)
        log.info(
            "Style changed config=%s hashes_invalidated=%s (attachment_id kept)",
            config_id,
            invalidated,
        )
    log.info("PATCH config=%s org=%s mode=%s", config_id, auth.organization_id, mode)
    return ConfigOut.from_record(rec)


async def _resolve_column_ids(
    settings: Settings,
    auth: AuthContext,
    worksheet_id: int,
    titles: list[str],
) -> list[int]:
    if settings.smartsheet_mode == "mock":
        return []
    import asyncio

    import smartsheet

    from smart_qr.adapters.smartsheet.credentials import get_credential_resolver

    credentials = await get_credential_resolver(settings).resolve(
        auth.organization_id, auth.user_id
    )
    wanted = {t.strip().lower(): t for t in titles if t and t.strip()}

    def _fetch() -> list[int]:
        client = smartsheet.Smartsheet(
            credentials.access_token, api_base=credentials.base_url
        )
        client.errors_as_exceptions(True)
        cols = client.Sheets.get_columns(worksheet_id, include_all=True)
        ids: list[int] = []
        for col in cols.data:
            key = (col.title or "").strip().lower()
            if key in wanted:
                ids.append(int(col.id))
        return ids

    return await asyncio.to_thread(_fetch)


@router.post("/configs/{config_id}/register-webhook")
async def register_webhook(
    config_id: UUID,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> dict:
    rec = await ConfigRepository().get(config_id, auth.organization_id)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    try:
        return await register_sheet_webhook(
            settings=settings,
            organization_id=auth.organization_id,
            worksheet_id=rec.worksheet_id,
            runner_user_id=rec.runner_user_id,
            acting_user_id=auth.user_id,
        )
    except CredentialsUnavailable as exc:
        raise _credentials_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("register-webhook failed: %s", exc)
        raise HTTPException(500, detail=str(exc)) from exc


@router.get("/configs/{config_id}", response_model=ConfigOut)
async def get_config(
    config_id: UUID, auth: AuthContext = Depends(require_auth)
) -> ConfigOut:
    rec = await ConfigRepository().get(config_id, auth.organization_id)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    return ConfigOut.from_record(rec)


@router.post("/configs/{config_id}/pause", response_model=ConfigOut)
async def pause_config(
    config_id: UUID, auth: AuthContext = Depends(require_auth)
) -> ConfigOut:
    rec = await ConfigRepository().set_pause(config_id, auth.organization_id, True)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    return ConfigOut.from_record(rec)


@router.post("/configs/{config_id}/resume", response_model=ConfigOut)
async def resume_config(
    config_id: UUID, auth: AuthContext = Depends(require_auth)
) -> ConfigOut:
    rec = await ConfigRepository().set_pause(config_id, auth.organization_id, False)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    return ConfigOut.from_record(rec)


@router.delete(
    "/configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_config(
    config_id: UUID,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> Response:
    repo = ConfigRepository()
    existing = await repo.get(config_id, auth.organization_id)
    if not existing:
        raise HTTPException(404, detail="Config not found")
    ok = await repo.delete(config_id, auth.organization_id)
    if not ok:
        raise HTTPException(404, detail="Config not found")

    # Last config for the sheet: stop Smartsheet from calling us for it.
    remaining = await repo.count_for_worksheet(
        auth.organization_id, existing.worksheet_id
    )
    if remaining == 0:
        try:
            await unregister_sheet_webhook(
                settings=settings,
                organization_id=auth.organization_id,
                worksheet_id=existing.worksheet_id,
                acting_user_id=auth.user_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Config %s deleted but webhook cleanup failed sheet=%s: %s",
                config_id,
                existing.worksheet_id,
                exc,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/configs/{config_id}/logo", response_model=ConfigOut)
async def upload_logo(
    config_id: UUID,
    auth: AuthContext = Depends(require_auth),
    file: UploadFile = File(...),
) -> ConfigOut:
    """Attach a center logo to this config (used when generating QR codes)."""
    rec = await ConfigRepository().get(config_id, auth.organization_id)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, detail="empty file")
    if len(raw) > 2_000_000:
        raise HTTPException(400, detail="logo must be under 2MB")
    ctype = (file.content_type or "").lower()
    if ctype not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise HTTPException(400, detail="logo must be PNG, JPEG, or WebP")
    # Normalize to PNG bytes for storage / render
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw)).convert("RGBA")
        buf = BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail=f"invalid image: {exc}") from exc

    updated = await ConfigRepository().set_logo(
        config_id,
        auth.organization_id,
        png_bytes=png_bytes,
        filename=(file.filename or "logo.png")[:200],
        content_type="image/png",
    )
    if not updated:
        raise HTTPException(404, detail="Config not found")
    # Logo is part of the QR payload — invalidate hashes so Generate rewrites.
    # Keep attachment_id so the next write can replace the printable file (SH-233).
    invalidated = await RowStateRepository().clear_for_config(config_id)
    log.info(
        "Logo uploaded config=%s bytes=%s file=%s hashes_invalidated=%s",
        config_id,
        len(png_bytes),
        file.filename,
        invalidated,
    )
    return ConfigOut.from_record(updated)


@router.delete("/configs/{config_id}/logo", response_model=ConfigOut)
async def delete_logo(
    config_id: UUID, auth: AuthContext = Depends(require_auth)
) -> ConfigOut:
    updated = await ConfigRepository().clear_logo(config_id, auth.organization_id)
    if not updated:
        raise HTTPException(404, detail="Config not found")
    invalidated = await RowStateRepository().clear_for_config(config_id)
    log.info(
        "Logo removed config=%s hashes_invalidated=%s (attachment_id kept)",
        config_id,
        invalidated,
    )
    return ConfigOut.from_record(updated)


@router.post("/qr-preview")
async def qr_preview(
    auth: AuthContext = Depends(require_auth),
    text: str = Form("SmartQR preview"),
    fill_color: str = Form("#000000"),
    back_color: str = Form("#FFFFFF"),
    box_size: int = Form(4),
    border: int = Form(2),
    logo_scale: float = Form(0.22),
    config_id: Optional[UUID] = Form(None),
    logo: Optional[UploadFile] = File(None),
) -> Response:
    """Render a sample QR with the requested style (wizard live preview)."""
    from smart_qr.domain.code_renderer import (
        RenderError,
        RenderOptions,
        clamp_border,
        clamp_box_size,
        clamp_logo_scale,
        normalize_hex_color,
        render_png,
    )

    logo_bytes = None
    if logo is not None:
        logo_bytes = await logo.read()
        if logo_bytes and len(logo_bytes) > 2_000_000:
            raise HTTPException(400, detail="logo must be under 2MB")
    if not logo_bytes and config_id is not None:
        logo_bytes = await ConfigRepository().get_logo_bytes(
            config_id, auth.organization_id
        )
    sample = (text or "SmartQR preview").strip()[:200] or "SmartQR preview"
    try:
        png = render_png(
            sample,
            RenderOptions(
                code_type="qr",
                qr_ecc="H",
                qr_box_size=clamp_box_size(box_size),
                qr_border=clamp_border(border),
                logo_png=logo_bytes or None,
                fill_color=normalize_hex_color(fill_color, "#000000"),
                back_color=normalize_hex_color(back_color, "#FFFFFF"),
                logo_scale=clamp_logo_scale(logo_scale),
            ),
        )
    except RenderError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    log.info(
        "QR preview org=%s user=%s bytes=%s",
        auth.organization_id,
        auth.user_id,
        len(png),
    )
    return Response(content=png, media_type="image/png")


@router.post("/configs/{config_id}/generate-row")
async def generate_row(
    config_id: UUID,
    body: GenerateRowRequest,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> dict:
    log.info(
        "generate-row config=%s row=%s idempotency=%s",
        config_id,
        body.row_id,
        idempotency_key,
    )
    await _assert_budget(auth, settings, 1)
    result = await (await _processor(settings)).process(
        organization_id=auth.organization_id,
        config_id=config_id,
        row_id=body.row_id,
        force=body.force,
        as_user_id=auth.user_id,
    )
    return {"status": result.status, "reason": result.reason}


@router.post("/configs/{config_id}/estimate-backfill")
async def estimate_backfill(
    config_id: UUID,
    body: EstimateBackfillRequest,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> dict:
    rec = await ConfigRepository().get(config_id, auth.organization_id)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    ss = await _smartsheet_for(auth, settings)
    row_ids = list(body.row_ids or [])
    discovered = False
    if not row_ids:
        row_ids = await ss.list_row_ids(rec.worksheet_id)
        discovered = True
    n = len(row_ids)
    quote = await (await get_billing(settings)).quote(auth.organization_id, n)
    log.info(
        "estimate-backfill config=%s rows=%s discovered=%s bolts=%s remaining=%s blocked=%s",
        config_id,
        n,
        discovered,
        quote.bolts_required,
        "unlimited" if quote.bolts_remaining is None else quote.bolts_remaining,
        quote.blocked_reason or "-",
    )
    return {
        "config_id": str(config_id),
        "rows_considered": n,
        "bolts_per_code": quote.bolts_per_code,
        "estimated_bolts": quote.bolts_required,
        "bolts_remaining": quote.bolts_remaining,
        "blocked_reason": quote.blocked_reason,
        "blocked_message": (
            BUDGET_MESSAGES.get(quote.blocked_reason) if quote.blocked_reason else None
        ),
        "row_ids": row_ids,
        "discovered_from_sheet": discovered,
        "note": (
            "Upper bound: rows skipped at runtime (unchanged hash, empty row, "
            "fill_if_empty) are never charged"
        ),
    }


@router.post("/configs/{config_id}/backfill")
async def backfill(
    config_id: UUID,
    body: BackfillRequest,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> dict:
    rec = await ConfigRepository().get(config_id, auth.organization_id)
    if not rec:
        raise HTTPException(404, detail="Config not found")
    ss = await _smartsheet_for(auth, settings)
    row_ids = list(body.row_ids or [])
    if not row_ids:
        row_ids = await ss.list_row_ids(rec.worksheet_id)
        log.info(
            "backfill discovered %s rows from sheet=%s", len(row_ids), rec.worksheet_id
        )

    # Fail fast when the target image column was deleted/renamed away
    if not await ss.sheet_has_column(rec.worksheet_id, int(rec.target_column_id)):
        log.error(
            "backfill aborted: target column missing sheet=%s column=%s config=%s",
            rec.worksheet_id,
            rec.target_column_id,
            config_id,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Target image column no longer exists on the sheet"
                + (
                    f" (was “{rec.target_column_title}”)"
                    if getattr(rec, "target_column_title", None)
                    else ""
                )
                + ". Edit this configuration and pick a valid column that still exists in Smartsheet."
            ),
        )

    await _assert_budget(auth, settings, len(row_ids))

    proc = await _processor(settings)
    results = []
    for row_id in row_ids:
        r = await proc.process(
            organization_id=auth.organization_id,
            config_id=config_id,
            row_id=row_id,
            force=body.force,
            as_user_id=auth.user_id,
        )
        results.append({"row_id": row_id, "status": r.status, "reason": r.reason})
    ok = sum(1 for x in results if x["status"] == "ok")
    skipped = sum(1 for x in results if x["status"] == "skipped")
    errors = sum(1 for x in results if x["status"] == "error")
    log.info(
        "backfill done config=%s total=%s ok=%s skipped=%s errors=%s",
        config_id,
        len(results),
        ok,
        skipped,
        errors,
    )
    charged = await (await get_billing(settings)).quote(auth.organization_id, ok)
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "ok": ok,
            "skipped": skipped,
            "errors": errors,
            # Only the rows actually written are charged.
            "estimated_bolts_charged": charged.bolts_required,
        },
    }


@router.post("/dev/inject-event")
async def inject_event(
    body: InjectEventRequest,
    auth: AuthContext = Depends(require_auth),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Local-only smoke helper. Authenticated and scoped to the caller's org."""
    if not settings.is_local:
        raise HTTPException(403, detail="inject-event only in local env")
    if body.organization_id != auth.organization_id:
        raise HTTPException(403, detail="organization mismatch")
    result = await (await _processor(settings)).process(
        organization_id=body.organization_id,
        config_id=body.config_id,
        row_id=body.row_id,
        changed_column_ids=body.changed_column_ids or None,
        change_agent=body.change_agent,
        force=body.force,
    )
    return {"status": result.status, "reason": result.reason}
