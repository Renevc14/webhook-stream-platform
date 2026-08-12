"""
Configuration Routes
====================

Endpoints for managing image-to-cell configurations.
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from pydantic import BaseModel, Field

from src.api.dependencies import (
    get_db,
    get_backfill_worker_dep,
    require_organization_id,
    get_user_id,
)
from src.services.supabase.client import SupabaseService
from src.services.supabase.config_repo import ConfigRepository
from src.workers.backfill import BackfillWorker, get_backfill_worker
from src.config.constants import TABLE_NAMES
from src.models.config import (
    ConfigCreate,
    ConfigType,
    ConfigUpdate,
    ImageToCellConfig,
    ProcessingMode,
)
import uuid


log = logging.getLogger(__name__)

router = APIRouter(prefix="/configs", tags=["Configurations"])


async def _register_webhook_for_config(
    worksheet_id: int,
    org_id: str,
    config_name: str,
    owner_user_id: Optional[str] = None,
) -> bool:
    """Ensure a Smartsheet webhook exists for a webhook-mode config.

    Pulse cutover (``tool_owns_webhook_lifecycle=False``): ask the gateway
    registrar via m2m ensure-registration — do not call Smartsheet from I2C.

    Legacy / local (tool still owns lifecycle): register via Smartsheet, save
    DB rows, then push the shared secret into the gateway Redis cache so the
    first callback does not 401 during the pull-sync window.
    """
    from src.config.settings import get_settings

    settings = get_settings()

    if not settings.tool_owns_webhook_lifecycle:
        return await _ensure_webhook_via_gateway(
            worksheet_id=worksheet_id,
            org_id=org_id,
            config_name=config_name,
        )

    return await _register_webhook_tool_owned(
        worksheet_id=worksheet_id,
        org_id=org_id,
        config_name=config_name,
        owner_user_id=owner_user_id,
    )


async def _ensure_webhook_via_gateway(
    *,
    worksheet_id: int,
    org_id: str,
    config_name: str,
) -> bool:
    """Pulse path: gateway registrar owns Smartsheet webhook create/heal."""
    from src.adapters.gateway import ensure_webhook_registration
    from src.config.settings import get_settings

    settings = get_settings()
    gateway_url = (settings.gateway_internal_url or "").strip()
    m2m = (settings.gateway_m2m_token or "").strip()
    if not gateway_url or not m2m:
        log.error(
            "[CONFIG:WEBHOOK] Pulse cutover: GATEWAY_INTERNAL_URL / "
            "GATEWAY_M2M_TOKEN missing — cannot ensure registration for '%s' "
            "(sheet=%s). Gateway poller will retry within WEBHOOK_LIFECYCLE_INTERVAL_S.",
            config_name,
            worksheet_id,
        )
        return False

    try:
        log.info(
            "[CONFIG:WEBHOOK] Pulse cutover: ensure-registration via gateway "
            "for config '%s' sheet=%s org=%s",
            config_name,
            worksheet_id,
            org_id,
        )
        await ensure_webhook_registration(
            gateway_base_url=gateway_url,
            m2m_token=m2m,
            tool_slug=settings.product_slug,
            organization_id=org_id,
            worksheet_id=worksheet_id,
        )
        return True
    except Exception as e:
        log.error(
            "[CONFIG:WEBHOOK] Gateway ensure-registration failed for '%s': %s",
            config_name,
            e,
        )
        return False


async def _register_webhook_tool_owned(
    *,
    worksheet_id: int,
    org_id: str,
    config_name: str,
    owner_user_id: Optional[str] = None,
) -> bool:
    """Tool-owned register (pre-cutover / local monolith)."""
    from src.adapters.gateway import push_webhook_secrets
    from src.config.settings import get_settings
    from src.services.smartsheet.client import get_smartsheet_client
    from src.services.smartsheet.webhooks import SmartsheetWebhookService
    from src.services.supabase.webhook_repo import WebhookRepository
    from src.models.webhook import WebhookRegistration

    settings = get_settings()
    callback_url = settings.resolve_webhook_callback_url(org_id)

    if not callback_url:
        log.error("[CONFIG:WEBHOOK] No callback URL configured, cannot register webhook")
        return False

    try:
        log.info(f"[CONFIG:WEBHOOK] Registering webhook immediately for config '{config_name}'")
        log.info(f"[CONFIG:WEBHOOK] Worksheet: {worksheet_id}, Callback: {callback_url}")

        # SH-178: webhooks on this sheet registered by OTHER orgs are visible
        # to us when the same Smartsheet user backs both orgs. The register
        # pre/post cleanup must not delete them. If the ownership lookup
        # fails we do NOT register (fail closed) — the lifecycle manager
        # retries within minutes with the full cross-org context.
        protected_ids: set = set()
        try:
            webhook_repo_ro = WebhookRepository()
            for row in webhook_repo_ro.get_by_worksheet_all_orgs(worksheet_id):
                if row.get("organization_id") != org_id and row.get("webhook_id"):
                    try:
                        protected_ids.add(int(row["webhook_id"]))
                    except (TypeError, ValueError):
                        pass
        except Exception as protect_err:
            log.error(
                f"[CONFIG:WEBHOOK] SH-178: could not load cross-org webhook "
                f"ownership for worksheet {worksheet_id}: {protect_err} — "
                f"NOT registering now (fail closed); lifecycle will retry"
            )
            return False

        webhook_service = SmartsheetWebhookService(
            get_smartsheet_client(org_id, owner_user_id)
        )
        webhook = await webhook_service.register_webhook(
            worksheet_id=worksheet_id,
            callback_url=callback_url,
            name=f"Image-to-Cell - {config_name}",
            protected_ids=protected_ids,
            ownership_patterns=settings.webhook_callback_ownership_patterns(),
        )

        webhook_repo = WebhookRepository()
        registration = WebhookRegistration(
            webhook_id=webhook.id,  # int
            worksheet_id=worksheet_id,
            organization_id=org_id,
            shared_secret=webhook.shared_secret or "",
            webhook_url=callback_url,
            status="active",
        )
        webhook_repo.save(registration)

        # Cold-start: push secret so gateway HMAC works before the next sync.
        gateway_url = (settings.gateway_internal_url or "").strip()
        m2m = (settings.gateway_m2m_token or "").strip()
        secret = (webhook.shared_secret or "").strip()
        if gateway_url and m2m and secret:
            try:
                await push_webhook_secrets(
                    gateway_base_url=gateway_url,
                    m2m_token=m2m,
                    tool_slug=settings.product_slug,
                    organization_id=org_id,
                    secrets={str(webhook.id): secret},
                )
            except Exception as push_err:
                log.error(
                    "[CONFIG:WEBHOOK] Gateway secret push failed for webhook=%s: %s "
                    "(DB save ok; gateway pull-sync will catch up)",
                    webhook.id,
                    push_err,
                )
        elif gateway_url or m2m:
            log.warning(
                "[CONFIG:WEBHOOK] Skip secret push — missing gateway URL/token "
                "or empty shared_secret for webhook=%s",
                webhook.id,
            )

        log.info(f"[CONFIG:WEBHOOK] Successfully registered webhook {webhook.id} for '{config_name}'")
        return True

    except Exception as e:
        log.error(f"[CONFIG:WEBHOOK] Failed to register webhook for '{config_name}': {e}")
        return False


class BackfillRequest(BaseModel):
    """Request to run backfill."""
    worksheet_id: int
    max_pages: Optional[int] = None


# SH-144: in-memory config payload used by the estimate endpoint's
# preview-before-commit path. Mirrors `ConfigCreate` so the frontend's
# Create New Config modal can serialise the same shape it would POST
# to `/configs` to create the config for real — the only fields skipped
# here are the ones supplied by the outer estimate request (worksheet_id)
# or by the auth layer (organization_id, created_by). Validation lives
# entirely in pydantic; FastAPI returns 422 on a malformed draft, same
# as it would for a malformed ConfigCreate.
class DraftConfigPayload(BaseModel):
    """Optional in-memory config to estimate alongside (or instead of)
    the existing persisted configs on the worksheet. Used by the
    Create New Config modal to preview the cost of a config that hasn't
    been saved yet (SH-144).
    """
    configuration_name: str = Field(..., min_length=1)
    processing_mode: ProcessingMode = ProcessingMode.WEBHOOK
    config_type: ConfigType = ConfigType.DEFAULT
    images_to_process: int = Field(default=1, ge=1, le=10)
    image_columns: List[int] = Field(..., min_length=1, max_length=10)
    # name_map mode only. Keys are 1-based slot indices matching the
    # corresponding image_columns entry; values are the "filename
    # contains" pattern for that slot. Slots omitted here fall back to
    # matching the column display name, same as a persisted config.
    namemap_patterns: Optional[Dict[int, str]] = None


class BackfillEstimateRequest(BackfillRequest):
    """Pre-backfill bolt usage estimate request.

    Same shape as ``BackfillRequest`` plus an optional ``draft_config``
    (SH-144). When ``draft_config`` is provided the estimator treats it
    as an in-memory config and returns its estimate in ``draft_estimate``
    alongside any estimates for configs already persisted on the
    worksheet. The draft is NOT written to the database — `compute_backfill_estimate`
    is read-only for this path.
    """
    draft_config: Optional[DraftConfigPayload] = None


# SH-121: shape for the pre-backfill estimate endpoint. The frontend uses this
# to show the user how many bolts the backfill would consume and how much of
# that would be overage before they decide to run it. The numbers come from
# `i2c_bolt_gate.compute_backfill_estimate`; both code paths (this endpoint and
# the existing /backfill enforcement) share that single estimator so they can
# never disagree.
class BackfillConfigEstimate(BaseModel):
    """Per-config backfill estimate."""
    config_id: str
    config_name: Optional[str] = None
    config_type: Optional[str] = None
    rows_with_attachments: int = 0
    images_needed: int = 0
    bolts_per_image: int = 1
    bolts_needed: int = 0
    bolts_available_in_cap: int = 0
    bolts_overage_estimated: int = 0
    has_ceiling: bool = False
    blocked: bool = False
    block_reason: Optional[str] = None
    error: Optional[str] = None


class BackfillEstimateTotals(BaseModel):
    """Org-level totals across all configs in the response.

    Note that ``bolts_available_in_cap`` is an org-level value (shared by every
    config), so the totals reflect "needed across all configs vs the org's
    single remaining cap" rather than naively summing per-config availability.
    """
    bolts_needed: int = 0
    bolts_available_in_cap: int = 0
    bolts_overage_estimated: int = 0
    any_blocked: bool = False


class DraftBackfillEstimate(BaseModel):
    """Per-draft backfill estimate (SH-144).

    Mirrors ``BackfillConfigEstimate`` minus the ``config_id`` field since
    the draft has no persisted id. Returned in the response's
    ``draft_estimate`` slot when the request body included ``draft_config``.
    """
    configuration_name: Optional[str] = None
    config_type: Optional[str] = None
    rows_with_attachments: int = 0
    images_needed: int = 0
    bolts_per_image: int = 1
    bolts_needed: int = 0
    bolts_available_in_cap: int = 0
    bolts_overage_estimated: int = 0
    has_ceiling: bool = False
    blocked: bool = False
    block_reason: Optional[str] = None
    error: Optional[str] = None


class BackfillEstimateResponse(BaseModel):
    """Response body for ``POST /configs/backfill/estimate``.

    Field naming reflects the two distinct sources of estimates (SH-144):

    - ``existing_configs`` — one entry per persisted config on the worksheet
      (this is the original SH-121 list).
    - ``draft_estimate`` — single entry for the in-memory draft from the
      request body's ``draft_config`` field, or ``None`` when the request
      did not include a draft. Used by the Create New Config modal.
    - ``totals`` — org-level aggregation across BOTH existing and draft
      (the math reflects "what would the org pay to run all of this").
    """
    worksheet_id: int
    existing_configs: List[BackfillConfigEstimate]
    draft_estimate: Optional[DraftBackfillEstimate] = None
    totals: BackfillEstimateTotals


@router.get("")
async def list_configs(
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> List[Dict[str, Any]]:
    """
    List all configurations for an organization.

    Args:
        org_id: Organization ID from header

    Returns:
        List of configuration objects
    """
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("organization_id", org_id)
            .execute(),
            "list configs"
        )
        return result.data or []
    except Exception as e:
        log.error(f"Failed to list configs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list configurations",
        )


@router.post("")
async def create_config(
    config: ConfigCreate,
    background_tasks: BackgroundTasks,
    org_id: str = Depends(require_organization_id),
    user_id: Optional[str] = Depends(get_user_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Create a new configuration.

    If processing_mode is "webhook", registers the webhook immediately
    to avoid waiting for the next healthcheck cycle (up to 5 minutes).

    Args:
        config: Configuration data
        background_tasks: FastAPI background tasks
        org_id: Organization ID from header
        user_id: User ID from header

    Returns:
        Created configuration object
    """
    try:
        # Build config data
        config_id = str(uuid.uuid4())
        config_data = {
            "id": config_id,
            "organization_id": org_id,
            "worksheet_id": config.worksheet_id,
            "configuration_name": config.configuration_name,
            "processing_mode": config.processing_mode.value,
            "config_type": config.config_type.value,
            "images_to_process": config.images_to_process,
            "paused": False,
            "created_by": user_id or config.created_by,
            # SH-178: the creator starts as the config's runner; failover or
            # an admin reassign can change it later.
            "runner_user_id": user_id or config.created_by,
        }

        # Add image columns
        for i, col_id in enumerate(config.image_columns, start=1):
            config_data[f"image{i}_column_id"] = col_id

        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .insert(config_data)
            .execute(),
            "create config"
        )

        if result.data:
            log.info(f"Created config {config_id} for worksheet {config.worksheet_id}")

            # Register webhook immediately for all webhook-based modes.
            if config.processing_mode.value in ("webhook", "webhook_v3"):
                log.info(f"[CONFIG:WEBHOOK] Config uses webhook mode, registering immediately...")

                # Register webhook in background to return response quickly
                # But use asyncio.create_task so it starts immediately (not after response)
                asyncio.create_task(
                    _register_webhook_for_config(
                        worksheet_id=config.worksheet_id,
                        org_id=org_id,
                        config_name=config.configuration_name,
                        owner_user_id=user_id or config.created_by,
                    )
                )

            # Trigger backfill only for the newly created config — not every
            # active sibling on the worksheet (worksheet-wide remains
            # POST /configs/backfill for ops).
            created_row = result.data[0]
            created_config = ImageToCellConfig(**created_row)
            log.info(
                f"[CONFIG:BACKFILL] Triggering backfill for config "
                f"{created_config.id} on worksheet {created_config.worksheet_id}"
            )
            asyncio.create_task(
                get_backfill_worker().run_for_single_config(created_config)
            )

            return created_row

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create configuration",
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to create config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create configuration: {str(e)}",
        )


@router.get("/{config_id}")
async def get_config(
    config_id: str,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get a specific configuration.

    Args:
        config_id: Configuration ID
        org_id: Organization ID from header

    Returns:
        Configuration object
    """
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .single()
            .execute(),
            "get config"
        )

        if not result.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found",
            )

        return result.data
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to get config {config_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get configuration",
        )


@router.put("/{config_id}")
async def update_config(
    config_id: str,
    config: ConfigUpdate,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Update a configuration.

    If processing_mode changes to "webhook" or paused changes to False,
    registers the webhook immediately.

    Args:
        config_id: Configuration ID
        config: Update data
        org_id: Organization ID from header

    Returns:
        Updated configuration object
    """
    try:
        # Get current config to check for webhook registration needs
        current_result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute(),
            "get current config"
        )

        current_config = current_result.data[0] if current_result.data else None
        if not current_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found",
            )

        # Build update data (only non-None fields)
        update_data = {}
        if config.configuration_name is not None:
            update_data["configuration_name"] = config.configuration_name
        if config.paused is not None:
            update_data["paused"] = config.paused
        if config.processing_mode is not None:
            update_data["processing_mode"] = config.processing_mode.value
        if config.config_type is not None:
            update_data["config_type"] = config.config_type.value
        if config.images_to_process is not None:
            update_data["images_to_process"] = config.images_to_process

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update",
            )

        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .update(update_data)
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .execute(),
            "update config"
        )

        if result.data:
            log.info(f"Updated config {config_id}")
            updated_config = result.data[0]

            # Check if we need to register webhook immediately
            # Cases:
            # 1. Mode changed to webhook (was something else)
            # 2. Paused changed to False and mode is webhook
            old_mode = current_config.get("processing_mode", "")
            new_mode = updated_config.get("processing_mode", old_mode)
            old_paused = current_config.get("paused", False)
            new_paused = updated_config.get("paused", old_paused)

            needs_webhook = False

            # Case 1: Mode changed to a webhook mode
            if new_mode in ("webhook", "webhook_v3") and old_mode not in ("webhook", "webhook_v3"):
                log.info(f"[CONFIG:WEBHOOK] Mode changed to {new_mode}, registering...")
                needs_webhook = True

            # Case 2: Unpaused and mode is a webhook mode
            elif new_mode in ("webhook", "webhook_v3") and old_paused and not new_paused:
                log.info(f"[CONFIG:WEBHOOK] Config unpaused in {new_mode} mode, registering...")
                needs_webhook = True

            if needs_webhook:
                # SH-178: webhook registration is background work — run as
                # the config's runner (created_by fallback).
                asyncio.create_task(
                    _register_webhook_for_config(
                        worksheet_id=updated_config["worksheet_id"],
                        org_id=org_id,
                        config_name=updated_config.get("configuration_name", "unknown"),
                        owner_user_id=(
                            updated_config.get("runner_user_id")
                            or updated_config.get("created_by")
                        ),
                    )
                )

            return updated_config

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configuration not found",
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to update config {config_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update configuration: {str(e)}",
        )


@router.delete("/{config_id}")
async def delete_config(
    config_id: str,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Delete a configuration.

    Args:
        config_id: Configuration ID
        org_id: Organization ID from header

    Returns:
        Deletion confirmation
    """
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .delete()
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .execute(),
            "delete config"
        )

        if result.data:
            log.info(f"Deleted config {config_id}")
            return {"deleted": True, "config_id": config_id}

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configuration not found",
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to delete config {config_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete configuration: {str(e)}",
        )


@router.post("/{config_id}/register-webhook")
async def register_webhook_for_config(
    config_id: str,
    org_id: str = Depends(require_organization_id),
    user_id: Optional[str] = Depends(get_user_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Trigger immediate webhook registration for a configuration.

    Called by the frontend after creating a new config via direct Supabase INSERT,
    so registration happens right away instead of waiting for the next lifecycle
    manager cycle (up to 5 minutes).

    Returns immediately; the actual registration runs in the background.
    """
    try:
        # SH-180: must select created_by — OAuth-only orgs resolve the
        # per-user broker token with owner_user_id. Omitting the column
        # made config.get("created_by") always None and create-time
        # register failed with "No Smartsheet credentials".
        # SH-178: also select runner_user_id — registration runs as the
        # config's runner when one is set.
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .select(
                "worksheet_id, configuration_name, processing_mode, "
                "paused, created_by, runner_user_id"
            )
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute(),
            "get config for webhook registration",
        )

        config = result.data[0] if result.data else None
        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Configuration not found",
            )

        if config.get("paused"):
            return {"status": "skipped", "reason": "Configuration is paused", "config_id": config_id}

        asyncio.create_task(
            _register_webhook_for_config(
                worksheet_id=config["worksheet_id"],
                org_id=org_id,
                config_name=config.get("configuration_name", "unknown"),
                owner_user_id=(
                    config.get("runner_user_id")
                    or config.get("created_by")
                    or user_id
                ),
            )
        )

        log.info(f"[CONFIG:WEBHOOK] Queued immediate webhook registration for config {config_id}")
        return {"status": "started", "config_id": config_id}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to register webhook for config {config_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to register webhook: {str(e)}",
        )


@router.post("/backfill")
async def run_backfill(
    request: BackfillRequest,
    background_tasks: BackgroundTasks,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
    worker: BackfillWorker = Depends(get_backfill_worker_dep),
) -> Dict[str, Any]:
    """
    Run backfill for a worksheet.

    Runs the bolt-budget preflight synchronously so the caller can surface
    per-config verdicts (e.g. "backfill skipped: insufficient bolts") in the
    UI. If at least one config on the worksheet passes preflight, the actual
    processing is kicked off in a background task. Blocked configs are NOT
    paused — they remain active for webhook-driven processing of future
    attachments.

    Args:
        request: Backfill request
        background_tasks: FastAPI background tasks
        org_id: Organization ID
        db: Supabase service
        worker: Backfill worker

    Returns:
        Dict with per-config verdicts and whether a background job was queued.
        On any unexpected preflight error we fail-open and queue the worker,
        preserving legacy behavior.
    """
    log.info(f"Backfill requested for worksheet {request.worksheet_id} org={org_id}")

    # Import heavy modules lazily so the router module stays cheap to import.
    from src.services.i2c_bolt_gate import preflight_backfill_budget
    from src.services.smartsheet.client import get_smartsheet_client
    from src.services.smartsheet.sheets import SheetService
    from src.config.settings import get_settings

    verdicts: List[Dict[str, Any]] = []
    preflight_ran = False

    try:
        config_repo = ConfigRepository(db)
        configs = config_repo.get_active_webhook_configs(request.worksheet_id)

        if not configs:
            log.info(
                f"No active webhook configs for worksheet {request.worksheet_id} — "
                "no backfill to run"
            )
            return {
                "status": "skipped",
                "worksheet_id": request.worksheet_id,
                "backfill_queued": False,
                "configs": [],
                "message": "No active configurations on this worksheet.",
            }

        # SH-100: async SheetService backed by smarthub_core SmartsheetClient.
        # Prefer the config owner's per-tool OAuth token (US/EU plans).
        sheet_service = SheetService(
            get_smartsheet_client(org_id, configs[0].created_by)
        )

        settings = get_settings()
        max_pages = request.max_pages or settings.backfill_max_pages
        rows_with_attachments = await sheet_service.get_rows_with_attachments(
            request.worksheet_id, max_pages
        )

        for config in configs:
            budget_issue = preflight_backfill_budget(
                config.organization_id,
                config,
                rows_with_attachments,
                sheet_service,
            )
            if budget_issue is None:
                verdicts.append({
                    "config_id": config.id,
                    "config_name": config.configuration_name,
                    "backfill_allowed": True,
                    "block_reason": None,
                })
            else:
                bolts_per_image = int(budget_issue.get("bolts_per_image") or 1)
                images_needed = int(budget_issue.get("images_needed") or 0)
                images_available = int(budget_issue.get("images_available") or 0)
                verdicts.append({
                    "config_id": config.id,
                    "config_name": config.configuration_name,
                    "backfill_allowed": False,
                    "block_reason": budget_issue.get("reason"),
                    "images_needed": images_needed,
                    "images_available": images_available,
                    "bolts_per_image": bolts_per_image,
                    "bolts_needed": images_needed * bolts_per_image,
                    "bolts_remaining": images_available * bolts_per_image,
                })

        preflight_ran = True

    except Exception as e:
        # Fail-open: if the synchronous preflight crashes for any reason
        # (Smartsheet API error, DB hiccup, etc.) we still queue the worker
        # so the caller sees legacy behavior rather than a hard failure.
        log.warning(
            f"Backfill preflight failed for worksheet {request.worksheet_id} "
            f"— falling back to queue-only: {e}"
        )

    any_allowed = any(v.get("backfill_allowed") for v in verdicts) if preflight_ran else True

    if any_allowed:
        background_tasks.add_task(
            worker.run_for_worksheet,
            request.worksheet_id,
            request.max_pages,
        )

    return {
        "status": "started" if any_allowed else "skipped",
        "worksheet_id": request.worksheet_id,
        "backfill_queued": any_allowed,
        "configs": verdicts,
    }


@router.post("/backfill/estimate", response_model=BackfillEstimateResponse)
async def estimate_backfill(
    request: BackfillEstimateRequest,
    org_id: str = Depends(require_organization_id),
    user_id: Optional[str] = Depends(get_user_id),
    db: SupabaseService = Depends(get_db),
) -> BackfillEstimateResponse:
    """
    Pre-backfill bolt usage preview (SH-121, extended for the Create modal
    flow in SH-144).

    Returns, per active webhook config on the worksheet, the projected images
    the backfill would process, the bolts they would consume, how much of that
    fits inside the org's remaining cap, and how much would be billed as
    overage. When the request body includes ``draft_config``, the same
    estimator runs on the in-memory draft and the result is returned in
    ``draft_estimate`` — the Create New Config modal uses this to show
    cost-before-commit. The endpoint is **read-only**: it does NOT enqueue
    the backfill and does NOT persist the draft.

    Both this endpoint and ``POST /configs/backfill`` share the same estimator
    (``i2c_bolt_gate.compute_backfill_estimate``) so the preview cannot
    disagree with the enforcement that runs at backfill time.

    Failure modes are surfaced explicitly per config rather than as HTTP errors
    so the UI can show partial results:
      - ``error="estimate_unavailable"`` — gate disabled, usage context could
        not be loaded, or the estimator crashed.
      - ``block_reason="prior_log_unavailable"`` — the prior-processing log
        could not be queried after retries; UI should ask the user to retry.
      - ``block_reason="backfill_budget_insufficient"`` — the backfill would
        exceed the org's hard ceiling (Free plans / paid plans with hard cap).
    """
    log.info(
        f"Backfill estimate requested for worksheet {request.worksheet_id} "
        f"org={org_id} draft={'yes' if request.draft_config else 'no'}"
    )

    # Lazy imports to keep the router module cheap.
    from src.services.i2c_bolt_gate import compute_backfill_estimate
    from src.services.smartsheet.client import get_smartsheet_client
    from src.services.smartsheet.sheets import SheetService
    from src.config.settings import get_settings

    config_repo = ConfigRepository(db)
    configs = config_repo.get_active_webhook_configs(request.worksheet_id)

    # Short-circuit only when there is nothing to estimate at all (no existing
    # configs AND no draft). The Create New Config modal will hit this code
    # path with `configs == []` AND a draft — keep going in that case.
    if not configs and request.draft_config is None:
        return BackfillEstimateResponse(
            worksheet_id=request.worksheet_id,
            existing_configs=[],
            draft_estimate=None,
            totals=BackfillEstimateTotals(),
        )

    try:
        # Prefer an existing config owner's OAuth token; for a draft-only
        # estimate the calling user is about to own the config, so use theirs.
        owner_user_id = (configs[0].created_by if configs else None) or user_id
        sheet_service = SheetService(get_smartsheet_client(org_id, owner_user_id))
        settings = get_settings()
        max_pages = request.max_pages or settings.backfill_max_pages
        rows_with_attachments = await sheet_service.get_rows_with_attachments(
            request.worksheet_id, max_pages
        )
    except Exception as e:
        # Upstream failure (Smartsheet 4xx/5xx, network) → 502 so the UI can
        # show a clear error rather than a misleading zero estimate.
        log.error(
            f"Backfill estimate: failed to fetch rows for worksheet "
            f"{request.worksheet_id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to fetch worksheet data from Smartsheet for the estimate.",
        )

    rows_count = len(rows_with_attachments)
    config_results: List[BackfillConfigEstimate] = []

    for config in configs:
        config_type_value = (
            getattr(getattr(config, "config_type", None), "value", None)
            or "default"
        )

        try:
            estimate = compute_backfill_estimate(
                config.organization_id, config, rows_with_attachments, sheet_service
            )
        except Exception as e:
            # Estimator should already swallow errors and return None, but guard
            # against any unexpected escape so one bad config does not break the
            # whole response.
            log.error(
                f"Backfill estimate: estimator crashed for config "
                f"{config.id}: {e}"
            )
            estimate = None

        if estimate is None:
            config_results.append(
                BackfillConfigEstimate(
                    config_id=config.id,
                    config_name=getattr(config, "configuration_name", None),
                    config_type=config_type_value,
                    rows_with_attachments=rows_count,
                    error="estimate_unavailable",
                )
            )
            continue

        config_results.append(
            BackfillConfigEstimate(
                config_id=config.id,
                config_name=getattr(config, "configuration_name", None),
                config_type=config_type_value,
                rows_with_attachments=rows_count,
                images_needed=int(estimate.get("images_needed") or 0),
                bolts_per_image=int(estimate.get("bolts_per_image") or 1),
                bolts_needed=int(estimate.get("bolts_needed") or 0),
                bolts_available_in_cap=int(estimate.get("bolts_available_in_cap") or 0),
                bolts_overage_estimated=int(estimate.get("bolts_overage_estimated") or 0),
                has_ceiling=bool(estimate.get("has_ceiling")),
                blocked=bool(estimate.get("blocked")),
                block_reason=estimate.get("block_reason"),
                error=estimate.get("error"),
            )
        )

    # SH-144: if the request included a draft_config, run the same estimator
    # on an in-memory ImageToCellConfig built from the draft payload. This is
    # the path the Create New Config modal uses to show cost-before-commit.
    # Validation lives in pydantic on DraftConfigPayload + the eventual
    # ImageToCellConfig instantiation; FastAPI returns 422 on a malformed
    # draft before we get here.
    draft_estimate: Optional[DraftBackfillEstimate] = None
    if request.draft_config is not None:
        draft_estimate = _estimate_draft_config(
            request.draft_config,
            org_id=org_id,
            worksheet_id=request.worksheet_id,
            rows_with_attachments=rows_with_attachments,
            rows_count=rows_count,
            compute_estimate=compute_backfill_estimate,
            sheet_service=sheet_service,
        )

    # Org-level totals. bolts_available_in_cap is org-wide (same value for
    # every config of the same org), so we take it from the first usable
    # estimate rather than summing. Overage is then needed-vs-cap at the org
    # level, which is what the UI should display. The draft, when present,
    # contributes to bolts_needed and any_blocked just like an existing
    # config — its cost is real money if the user clicks Save.
    total_needed = sum(c.bolts_needed for c in config_results)
    if draft_estimate is not None:
        total_needed += draft_estimate.bolts_needed
    cap_values = [
        c.bolts_available_in_cap for c in config_results if c.error is None
    ]
    if (
        not cap_values
        and draft_estimate is not None
        and draft_estimate.error is None
    ):
        cap_values = [draft_estimate.bolts_available_in_cap]
    bolts_available_in_cap = cap_values[0] if cap_values else 0
    any_blocked = any(c.blocked for c in config_results) or (
        draft_estimate is not None and draft_estimate.blocked
    )
    totals = BackfillEstimateTotals(
        bolts_needed=total_needed,
        bolts_available_in_cap=bolts_available_in_cap,
        bolts_overage_estimated=max(0, total_needed - bolts_available_in_cap),
        any_blocked=any_blocked,
    )

    return BackfillEstimateResponse(
        worksheet_id=request.worksheet_id,
        existing_configs=config_results,
        draft_estimate=draft_estimate,
        totals=totals,
    )


def _estimate_draft_config(
    draft: DraftConfigPayload,
    *,
    org_id: str,
    worksheet_id: int,
    rows_with_attachments: List[Dict[str, Any]],
    rows_count: int,
    compute_estimate,
    sheet_service,
) -> DraftBackfillEstimate:
    """SH-144: build an in-memory ImageToCellConfig from the draft and run
    the shared estimator on it. This function does no I/O beyond what
    ``compute_backfill_estimate`` already does (which is read-only — usage
    context lookup, prior-log read, mode evaluation) and never persists the
    draft.
    """
    # Mirror the same field-mapping logic that ``POST /configs`` uses when
    # turning a ConfigCreate into a row, just keep the result in memory.
    draft_id = "draft-preview"
    fields: Dict[str, Any] = {
        "id": draft_id,
        "organization_id": org_id,
        "worksheet_id": worksheet_id,
        "configuration_name": draft.configuration_name,
        "processing_mode": draft.processing_mode,
        "config_type": draft.config_type,
        "images_to_process": draft.images_to_process,
    }
    for i, col_id in enumerate(draft.image_columns, start=1):
        fields[f"image{i}_column_id"] = col_id
    if draft.namemap_patterns:
        for slot_index, pattern in draft.namemap_patterns.items():
            fields[f"namemap_column{slot_index}"] = pattern

    try:
        draft_cfg = ImageToCellConfig(**fields)
    except Exception as e:
        # Pydantic should already catch shape errors on DraftConfigPayload,
        # but if a constraint slips through (e.g., an out-of-range slot
        # index in namemap_patterns mapping to a missing namemap_column field),
        # we surface it as an explicit estimate-unavailable rather than a 500.
        log.error(
            "Backfill estimate: failed to materialise draft config: %s", e
        )
        return DraftBackfillEstimate(
            configuration_name=draft.configuration_name,
            config_type=draft.config_type.value if draft.config_type else "default",
            rows_with_attachments=rows_count,
            error="estimate_unavailable",
        )

    try:
        estimate = compute_estimate(
            org_id, draft_cfg, rows_with_attachments, sheet_service
        )
    except Exception as e:
        log.error(
            "Backfill estimate: estimator crashed for draft config: %s", e
        )
        estimate = None

    if estimate is None:
        return DraftBackfillEstimate(
            configuration_name=draft.configuration_name,
            config_type=draft.config_type.value,
            rows_with_attachments=rows_count,
            error="estimate_unavailable",
        )

    return DraftBackfillEstimate(
        configuration_name=draft.configuration_name,
        config_type=draft.config_type.value,
        rows_with_attachments=rows_count,
        images_needed=int(estimate.get("images_needed") or 0),
        bolts_per_image=int(estimate.get("bolts_per_image") or 1),
        bolts_needed=int(estimate.get("bolts_needed") or 0),
        bolts_available_in_cap=int(estimate.get("bolts_available_in_cap") or 0),
        bolts_overage_estimated=int(estimate.get("bolts_overage_estimated") or 0),
        has_ceiling=bool(estimate.get("has_ceiling")),
        blocked=bool(estimate.get("blocked")),
        block_reason=estimate.get("block_reason"),
        error=estimate.get("error"),
    )


# ═════════════════════════════════════════════════════════════════════
#  SH-183 — config-scoped manual Run Backfill (estimate + execute)
# ═════════════════════════════════════════════════════════════════════
# The worksheet-scoped POST /configs/backfill runs EVERY active config on a
# worksheet, so it is not safe to bind to a single configuration's button.
# These routes select exactly one config, run as the authenticated session
# user (interactive flow — never created_by / runner_user_id), and secure
# the concurrency lease before returning 202.

class ConfigBackfillOptions(BaseModel):
    """Optional body for the config-scoped backfill routes."""
    max_pages: Optional[int] = None


class ConfigBackfillEstimateResponse(BaseModel):
    """Response for ``POST /configs/{config_id}/backfill/estimate``.

    Shares ``i2c_bolt_gate.compute_backfill_estimate`` with runtime
    enforcement so preview and execution cannot disagree.
    """
    config_id: str
    config_name: Optional[str] = None
    config_type: Optional[str] = None
    worksheet_id: int
    rows_with_attachments: int = 0
    images_needed: int = 0
    bolts_per_image: int = 1
    bolts_needed: int = 0
    bolts_available_in_cap: int = 0
    has_hard_cap: bool = False
    plan_type: Optional[str] = None
    is_free_plan: bool = False
    overage_allowed: bool = False
    bolts_overage_estimated: int = 0
    overage_rate_per_bolt: Optional[float] = None
    blocked: bool = False
    block_reason: Optional[str] = None
    error: Optional[str] = None
    guidance: Optional[str] = None


def _run_backfill_error(status_code: int, error_code: str, message: str,
                        **extra: Any) -> HTTPException:
    """Typed error payload for the SH-183 routes: stable machine-readable
    ``error_code`` plus a human message, without leaking cross-org detail."""
    return HTTPException(
        status_code=status_code,
        detail={"error_code": error_code, "message": message, **extra},
    )


def _require_session_user(request: Request, x_user_id: Optional[str]) -> str:
    """Authenticated session user for the interactive backfill flow.

    The Supabase JWT gate middleware has already verified the Bearer token
    and rejected any X-User-ID that does not match the token's ``sub``, so
    ``i2c_jwt_sub`` is authoritative. The header is only accepted in legacy
    mode (no JWT secret configured — local/dev).
    """
    user_id = getattr(request.state, "i2c_jwt_sub", None) or (x_user_id or "").strip()
    if not user_id:
        raise _run_backfill_error(
            status.HTTP_401_UNAUTHORIZED,
            "auth_required",
            "A valid Hub session is required to run a backfill.",
        )
    return str(user_id)


def _load_config_row_for_backfill(
    db: SupabaseService, config_id: str, org_id: str
) -> Dict[str, Any]:
    """Fetch the config row scoped to the JWT organization.

    Cross-org and nonexistent ids both return the same 404 so existence is
    never leaked across tenants.
    """
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
            .select("*")
            .eq("id", config_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute(),
            "get config for backfill",
        )
    except Exception as e:
        log.error(f"SH-183: config lookup failed for {config_id}: {e}")
        raise _run_backfill_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "config_lookup_failed",
            "Failed to load the configuration.",
        )
    row = result.data[0] if result.data else None
    if not row:
        raise _run_backfill_error(
            status.HTTP_404_NOT_FOUND,
            "config_not_found",
            "Configuration not found.",
        )
    return row


def _require_backfill_permission(
    db: SupabaseService, user_id: str, org_id: str, config_row: Dict[str, Any]
) -> None:
    """Membership + the same edit/admin rule the config UI enforces.

    Mirrors the frontend ``canEditToolConfig``: org admin / prodactive
    admin-staff, OR the config is shared with the org, OR the caller owns it
    (``user_id`` after transfers, else ``created_by``). Fail-closed: lookup
    errors deny rather than allow a billable action.
    """
    try:
        m = db.table("user_organization_memberships") \
            .select("user_id, is_active") \
            .eq("user_id", user_id) \
            .eq("organization_id", org_id) \
            .limit(1).execute()
        member_row = m.data[0] if m.data else None
    except Exception as e:
        log.error(f"SH-183: membership lookup failed user={user_id}: {e}")
        member_row = None
    if not member_row or member_row.get("is_active") is False:
        raise _run_backfill_error(
            status.HTTP_403_FORBIDDEN,
            "not_org_member",
            "You are not an active member of this organization.",
        )

    if config_row.get("shared_with_org"):
        return
    owner_id = config_row.get("user_id") or config_row.get("created_by")
    if owner_id and str(owner_id) == str(user_id):
        return

    try:
        p = db.table("user_profiles").select("role, is_admin") \
            .eq("id", user_id).limit(1).execute()
        profile = p.data[0] if p.data else {}
    except Exception as e:
        log.error(f"SH-183: profile lookup failed user={user_id}: {e}")
        profile = {}
    role = (profile.get("role") or "").strip().lower().replace(" ", "_")
    if role in ("admin", "prodactive_admin", "prodactive_staff") or profile.get("is_admin"):
        return

    raise _run_backfill_error(
        status.HTTP_403_FORBIDDEN,
        "forbidden",
        "You do not have permission to run this configuration.",
    )


def _require_runnable(config_row: Dict[str, Any]) -> None:
    """Reject paused or otherwise non-runnable configurations."""
    if config_row.get("paused"):
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            "config_paused",
            "This configuration is paused. Resume it before running a backfill.",
        )
    mode = (config_row.get("processing_mode") or "").strip().lower()
    if mode not in ("webhook", "webhook_v3"):
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            "config_not_runnable",
            "Backfill is only available for webhook configurations.",
        )


from src.services.runner_failover import classify_credential_failure as _classify_credential_failure


def _raise_for_sheet_fetch_failure(exc: Exception, config_id: str) -> None:
    """Map a Smartsheet fetch failure to the documented typed responses."""
    cred_state = _classify_credential_failure(exc)
    if cred_state == "reconnect_required":
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            "reconnect_required",
            "Your Smartsheet connection was revoked. Reconnect the tool via "
            "Profile → Connect, then retry.",
        )
    if cred_state == "connection_missing":
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            "connection_missing",
            "Smartsheet is not connected for your account. Connect the tool "
            "via Profile → Connect, then retry.",
        )
    log.error(f"SH-183: worksheet fetch failed for config {config_id}: {exc}")
    raise _run_backfill_error(
        status.HTTP_502_BAD_GATEWAY,
        "smartsheet_unavailable",
        "Unable to fetch worksheet data from Smartsheet. Retry shortly.",
    )


def _fetch_overage_rate(db: SupabaseService, org_id: str) -> Optional[float]:
    """The plan's normal overage rate per bolt, for pre-run disclosure."""
    try:
        r = db.table("organization_subscriptions") \
            .select("overage_rate_per_bolt") \
            .eq("organization_id", org_id).limit(1).execute()
        if r.data and r.data[0].get("overage_rate_per_bolt") is not None:
            return float(r.data[0]["overage_rate_per_bolt"])
    except Exception as e:
        log.warning(f"SH-183: overage rate lookup failed org={org_id}: {e}")
    return None


def _estimate_guidance(
    *,
    blocked: bool,
    block_reason: Optional[str],
    error: Optional[str],
    is_free_plan: bool,
    has_hard_cap: bool,
    overage_bolts: int,
) -> Optional[str]:
    """Actionable retry / reconnect / top-up guidance for the confirmation UI."""
    if error == "estimate_unavailable":
        return "The estimate is temporarily unavailable. Retry shortly."
    if block_reason == "prior_log_unavailable":
        return ("Processing history could not be read, so the cost cannot be "
                "verified. Retry shortly — no work was queued.")
    if blocked and is_free_plan:
        return ("Free plans cannot buy additional credits. Wait for your "
                "monthly reset or upgrade your plan to run this backfill.")
    if blocked and has_hard_cap:
        return ("Your plan's hard cap prevents this backfill. Top up your "
                "plan or wait for the monthly cycle reset.")
    if overage_bolts > 0:
        return ("This run exceeds your remaining monthly bolts. The excess "
                "is billed as overage at your plan's normal overage rate.")
    return None


@router.post(
    "/{config_id}/backfill/estimate",
    response_model=ConfigBackfillEstimateResponse,
)
async def estimate_config_backfill(
    config_id: str,
    request: Request,
    options: Optional[ConfigBackfillOptions] = None,
    org_id: str = Depends(require_organization_id),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    db: SupabaseService = Depends(get_db),
) -> ConfigBackfillEstimateResponse:
    """Config-scoped pre-backfill estimate (SH-183).

    Read-only: never queues work. Runs the same estimator as execution-time
    enforcement, selecting exactly the requested configuration, with
    Smartsheet access resolved as the **authenticated session user**.

    Typed failures: 401 ``auth_required``, 403 ``not_org_member`` /
    ``forbidden``, 404 ``config_not_found``, 409 ``config_paused`` /
    ``config_not_runnable`` / ``reconnect_required`` /
    ``connection_missing``, 502 ``smartsheet_unavailable``. Estimator
    soft-failures come back as 200 with ``error="estimate_unavailable"`` or
    ``block_reason="prior_log_unavailable"`` — never a fabricated zero.
    """
    from src.services.bolt_cap import load_i2c_org_usage_context
    from src.services.i2c_bolt_gate import compute_backfill_estimate
    from src.services.smartsheet.client import get_smartsheet_client
    from src.services.smartsheet.sheets import SheetService
    from src.config.settings import get_settings

    user_id = _require_session_user(request, x_user_id)
    config_row = _load_config_row_for_backfill(db, config_id, org_id)
    _require_backfill_permission(db, user_id, org_id, config_row)
    _require_runnable(config_row)

    config = ImageToCellConfig(**config_row)
    config_type_value = (
        getattr(getattr(config, "config_type", None), "value", None) or "default"
    )

    # Interactive flow: the session user's connection — never created_by.
    try:
        sheet_service = SheetService(get_smartsheet_client(org_id, user_id))
        settings = get_settings()
        max_pages = (options.max_pages if options else None) or settings.backfill_max_pages
        rows_with_attachments = await sheet_service.get_rows_with_attachments(
            config.worksheet_id, max_pages
        )
    except Exception as e:
        _raise_for_sheet_fetch_failure(e, config_id)

    ctx = load_i2c_org_usage_context(org_id) or {}
    plan_type = ctx.get("plan_type")
    is_free_plan = bool(ctx.get("is_free_plan"))
    overage_rate = _fetch_overage_rate(db, org_id)

    try:
        estimate = compute_backfill_estimate(
            org_id, config, rows_with_attachments, sheet_service
        )
    except Exception as e:
        log.error(f"SH-183: estimator crashed for config {config_id}: {e}")
        estimate = None

    rows_count = len(rows_with_attachments)

    if estimate is None:
        # Never fabricate zero cost: an explicit retryable state instead.
        return ConfigBackfillEstimateResponse(
            config_id=config.id,
            config_name=config.configuration_name,
            config_type=config_type_value,
            worksheet_id=config.worksheet_id,
            rows_with_attachments=rows_count,
            plan_type=plan_type,
            is_free_plan=is_free_plan,
            overage_rate_per_bolt=overage_rate,
            blocked=True,
            block_reason="estimate_unavailable",
            error="estimate_unavailable",
            guidance=_estimate_guidance(
                blocked=True, block_reason=None, error="estimate_unavailable",
                is_free_plan=is_free_plan, has_hard_cap=False, overage_bolts=0,
            ),
        )

    has_hard_cap = bool(estimate.get("has_ceiling"))
    blocked = bool(estimate.get("blocked"))
    block_reason = estimate.get("block_reason")
    overage_bolts = int(estimate.get("bolts_overage_estimated") or 0)
    overage_allowed = not has_hard_cap and not is_free_plan

    return ConfigBackfillEstimateResponse(
        config_id=config.id,
        config_name=config.configuration_name,
        config_type=config_type_value,
        worksheet_id=config.worksheet_id,
        rows_with_attachments=rows_count,
        images_needed=int(estimate.get("images_needed") or 0),
        bolts_per_image=int(estimate.get("bolts_per_image") or 1),
        bolts_needed=int(estimate.get("bolts_needed") or 0),
        bolts_available_in_cap=int(estimate.get("bolts_available_in_cap") or 0),
        has_hard_cap=has_hard_cap,
        plan_type=plan_type,
        is_free_plan=is_free_plan,
        overage_allowed=overage_allowed,
        bolts_overage_estimated=overage_bolts,
        overage_rate_per_bolt=overage_rate,
        blocked=blocked,
        block_reason=block_reason,
        error=estimate.get("error"),
        guidance=_estimate_guidance(
            blocked=blocked, block_reason=block_reason, error=estimate.get("error"),
            is_free_plan=is_free_plan, has_hard_cap=has_hard_cap,
            overage_bolts=overage_bolts if overage_allowed else 0,
        ),
    )


@router.post("/{config_id}/backfill", status_code=status.HTTP_202_ACCEPTED)
async def run_config_backfill(
    config_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    options: Optional[ConfigBackfillOptions] = None,
    org_id: str = Depends(require_organization_id),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
    db: SupabaseService = Depends(get_db),
    worker: BackfillWorker = Depends(get_backfill_worker_dep),
) -> Dict[str, Any]:
    """Run a backfill for exactly one configuration (SH-183).

    Returns 202 only after the request is authorized, the budget/cap has
    been revalidated (the estimate is not a reservation), and the worksheet
    concurrency lease is secured. The pre-acquired lease is handed to the
    background worker, which heartbeat-extends and finally releases it. A
    202 means accepted/in progress — not completed.

    Typed 409s: ``backfill_already_running``, ``config_paused``,
    ``config_not_runnable``, ``reconnect_required``, ``connection_missing``,
    ``backfill_budget_insufficient``, ``prior_log_unavailable``, and the
    hard-cap lockout reasons from the bolt gate (``plan_suspended``,
    ``bolts_pending_payment``, ``hard_cap_reached_flag``,
    ``bolt_ceiling_already_met``, ``insufficient_bolts_for_minimum_action``).
    """
    from src.services.backfill_lock import (
        acquire_worksheet_backfill_lease,
        release_backfill_lease,
    )
    from src.services.i2c_bolt_gate import (
        preflight_backfill_budget,
        preflight_pause_if_blocked,
    )
    from src.services.smartsheet.client import get_smartsheet_client
    from src.services.smartsheet.sheets import SheetService
    from src.config.settings import get_settings

    user_id = _require_session_user(request, x_user_id)
    config_row = _load_config_row_for_backfill(db, config_id, org_id)
    _require_backfill_permission(db, user_id, org_id, config_row)
    _require_runnable(config_row)

    config = ImageToCellConfig(**config_row)

    # Hard-cap / suspension lockout — revalidated at execution time, not
    # trusted from the earlier estimate.
    block = preflight_pause_if_blocked(org_id, config.id)
    if block:
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            block,
            f"Backfill blocked ({block}). Top up your plan or wait for the "
            "monthly cycle reset.",
        )

    settings = get_settings()

    # Secure the concurrency lease BEFORE the (slow) worksheet fetch so an
    # overlapping backfill fails fast with an actionable 409.
    lease = await acquire_worksheet_backfill_lease(
        config.worksheet_id,
        ttl_s=settings.backfill_lock_ttl_s,
        heartbeat_interval_s=settings.backfill_lock_heartbeat_s,
    )
    if lease is None:
        raise _run_backfill_error(
            status.HTTP_409_CONFLICT,
            "backfill_already_running",
            "A backfill is already running for this worksheet. Wait for it "
            "to finish, then retry.",
        )

    try:
        # Interactive flow: the session user's connection — never created_by.
        try:
            sheet_service = SheetService(get_smartsheet_client(org_id, user_id))
            max_pages = (options.max_pages if options else None) or settings.backfill_max_pages
            rows_with_attachments = await sheet_service.get_rows_with_attachments(
                config.worksheet_id, max_pages
            )
        except Exception as e:
            _raise_for_sheet_fetch_failure(e, config_id)

        # Budget revalidation at execution time (state may have changed
        # since the estimate — the estimate is not a reservation).
        budget_issue = preflight_backfill_budget(
            org_id, config, rows_with_attachments, sheet_service
        )
        if budget_issue is not None:
            reason = budget_issue.get("reason") or "backfill_budget_insufficient"
            raise _run_backfill_error(
                status.HTTP_409_CONFLICT,
                reason,
                "The backfill can no longer run within your plan's budget."
                if reason == "backfill_budget_insufficient"
                else "Processing history could not be verified. Retry shortly.",
                images_needed=int(budget_issue.get("images_needed") or 0),
                images_available=int(budget_issue.get("images_available") or 0),
                bolts_per_image=int(budget_issue.get("bolts_per_image") or 1),
            )
    except Exception:
        # Any failure between lease acquisition and worker handoff must
        # release the lease or the worksheet stays locked for the full TTL.
        await release_backfill_lease(lease)
        raise

    run_id = str(uuid.uuid4())
    background_tasks.add_task(
        worker.run_for_config,
        config,
        rows_with_attachments,
        sheet_service,
        lease=lease,
        session_user_id=user_id,
        run_id=run_id,
    )

    log.info(
        f"SH-183: accepted config-scoped backfill run={run_id} "
        f"config={config.id} worksheet={config.worksheet_id} user={user_id}"
    )
    return {
        "status": "accepted",
        "run_id": run_id,
        "config_id": config.id,
        "worksheet_id": config.worksheet_id,
        "message": "Backfill accepted",
    }


@router.get("/{config_id}/stats")
async def get_config_stats(
    config_id: str,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get processing statistics for a configuration.

    Args:
        config_id: Configuration ID
        org_id: Organization ID

    Returns:
        Statistics object
    """
    try:
        # Get row states for this config
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.ROW_STATES)
            .select("is_locked, images_processed, last_processed_at")
            .eq("config_id", config_id)
            .execute(),
            "get config stats"
        )

        rows = result.data or []

        total_rows = len(rows)
        locked_rows = sum(1 for r in rows if r.get("is_locked"))
        total_images = sum(r.get("images_processed", 0) for r in rows)

        return {
            "config_id": config_id,
            "total_rows_processed": total_rows,
            "locked_rows": locked_rows,
            "total_images_processed": total_images,
        }
    except Exception as e:
        log.error(f"Failed to get stats for config {config_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get statistics",
        )
