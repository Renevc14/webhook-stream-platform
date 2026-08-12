"""
Image-to-Cell bolt / hard-cap preflight (aligns with attachment-counter bolt_gate).

Before any billable work: if the org cannot afford one priced image (pricing_list.primary_event_bolts)
or is blocked (suspended, pending payment, hard-cap flag, ceiling), pause configs and skip the job.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from src.services.bolt_cap import load_i2c_org_usage_context
from src.services.supabase.client import get_supabase_service

log = logging.getLogger(__name__)


class PriorLogUnavailable(Exception):
    """Raised when imagetocell_usage_log cannot be queried after retries.

    Callers must NOT silently treat this as 'no prior history' — that path
    causes overcharge by re-billing already-processed images. Backfill
    callers should abort with a clear, retryable error message; per-event
    callers may choose to fail-open (the blast radius for one event is
    bounded to one attachment).
    """


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def i2c_bolt_gate_enabled() -> bool:
    return _env_bool("I2C_BOLT_GATE_ENABLED", "true")


def pause_all_imagetocell_configs(organization_id: str, reason: str = "") -> None:
    try:
        sb = get_supabase_service().client
        sb.rpc(
            "pause_all_imagetocell_configs_for_org",
            {"p_organization_id": organization_id, "p_reason": reason or None},
        ).execute()
        log.warning(
            "i2c_bolt_gate: paused ALL imagetocell configs org=%s reason=%s",
            organization_id,
            reason,
        )
    except Exception as e:
        log.error("i2c_bolt_gate: pause_all RPC failed org=%s: %s", organization_id, e)
        try:
            get_supabase_service().table("imagetocell_config").update({"paused": True}).eq(
                "organization_id", organization_id
            ).eq("paused", False).execute()
        except Exception as e2:
            log.error("i2c_bolt_gate: pause_all fallback failed org=%s: %s", organization_id, e2)


def pause_one_imagetocell_config(organization_id: str, config_id: str, reason: str = "") -> None:
    try:
        sb = get_supabase_service().client
        sb.rpc(
            "pause_imagetocell_config_by_id",
            {
                "p_config_id": config_id,
                "p_organization_id": organization_id,
                "p_reason": reason or None,
            },
        ).execute()
        log.warning(
            "i2c_bolt_gate: paused config=%s org=%s reason=%s",
            config_id,
            organization_id,
            reason,
        )
    except Exception as e:
        log.error("i2c_bolt_gate: pause_one RPC failed config=%s: %s", config_id, e)
        try:
            get_supabase_service().table("imagetocell_config").update({"paused": True}).eq(
                "id", config_id
            ).eq("organization_id", organization_id).execute()
        except Exception as e2:
            log.error("i2c_bolt_gate: pause_one fallback failed config=%s: %s", config_id, e2)


def preflight_pause_if_blocked(organization_id: str, config_id: Optional[str] = None) -> Optional[str]:
    """
    Run before fetching sheets / processing rows. If blocked, pause org configs and return reason.
    Returns None when processing may proceed (or gate disabled).
    When the Hub budget cannot be loaded, returns ``budget_unavailable``
    (fail-closed — same policy as SmartQR / get_remaining_image_capacity).
    """
    if not i2c_bolt_gate_enabled():
        return None

    ctx = load_i2c_org_usage_context(organization_id)
    if ctx is None:
        # Align with get_remaining_image_capacity (fail-closed) and SmartQR
        # BoltGate: never spend when the Hub budget cannot be read.
        log.error(
            "i2c_bolt_gate: no context for org=%s — refusing job (fail-closed)",
            organization_id,
        )
        return "budget_unavailable"

    if ctx.get("plan_suspended"):
        pause_all_imagetocell_configs(organization_id, "plan_suspended")
        return "plan_suspended"

    if ctx.get("bolts_pending_payment"):
        pause_all_imagetocell_configs(organization_id, "bolts_pending_payment")
        return "bolts_pending_payment"

    if ctx.get("hard_cap_reached_rpc"):
        pause_all_imagetocell_configs(organization_id, "hard_cap_reached_subscription_flag")
        return "hard_cap_reached_flag"

    ceiling = ctx.get("ceiling_bolts")
    consumed_start = int(ctx.get("consumed_bolts_start") or 0)
    if ceiling is not None and consumed_start >= ceiling:
        pause_all_imagetocell_configs(organization_id, "bolt_ceiling_already_met")
        return "bolt_ceiling_already_met"

    remaining_eff = ctx.get("remaining_effective_bolts")
    bolts_per_image = int(ctx.get("bolts_per_image") or 1)
    if bolts_per_image < 1:
        bolts_per_image = 1

    if remaining_eff is not None and remaining_eff < bolts_per_image:
        pause_all_imagetocell_configs(organization_id, "insufficient_bolts_for_minimum_action")
        return "insufficient_bolts_for_minimum_action"

    # config_id reserved for future per-config pause paths
    _ = config_id
    return None


_PRIOR_LOG_RETRY_ATTEMPTS = 3
_PRIOR_LOG_RETRY_BASE_DELAY_S = 1.0

# Chunking constants. Both PostgREST and most ALB / proxy stacks reject
# query strings beyond ~8KB. With UUIDs and BIGINTs typically running
# ~16-20 chars each, 500 row_ids per IN() clause keeps us well under that
# limit even when the query is otherwise long.
#
# The page size matters for response volume, not request size: PostgREST
# defaults to a max of 1000 rows per response, so requesting 500 stays
# under that threshold even when chunked queries return more than the
# row_ids count (eg. replace mode with many historical entries per row).
_PRIOR_LOG_ROW_ID_CHUNK_SIZE = 500
_PRIOR_LOG_PAGE_SIZE = 500


def _fetch_prior_processed_inner(
    organization_id: str,
    worksheet_id: int,
    target_column_ids: List[int],
    row_ids: List[int],
) -> Dict[int, List[Tuple[int, str]]]:
    """Single-shot fetch — raises on any DB error so the caller can retry.

    Chunks `row_ids` into batches of _PRIOR_LOG_ROW_ID_CHUNK_SIZE so the
    `IN()` clause never produces a URL longer than what proxies and
    PostgREST accept (B17 fix).

    Within each chunk, paginates the response with `range(start, end)` in
    pages of _PRIOR_LOG_PAGE_SIZE rows so PostgREST's default 1000-row
    response cap never silently truncates results (B14 fix). Truncation
    used to make rows that had >1000 prior entries appear unprocessed,
    which re-billed already-processed images.
    """
    db = get_supabase_service()
    out: Dict[int, List[Tuple[int, str]]] = {}

    # B17: split row_ids into chunks so the IN() clause stays small.
    for chunk_start in range(0, len(row_ids), _PRIOR_LOG_ROW_ID_CHUNK_SIZE):
        chunk = row_ids[chunk_start : chunk_start + _PRIOR_LOG_ROW_ID_CHUNK_SIZE]

        # B14: paginate response with range(). Loop until the page is
        # smaller than the page size (no more rows).
        offset = 0
        while True:
            result = (
                db.table("imagetocell_usage_log")
                .select("row_id, image_column_id, image_filename")
                .eq("organization_id", organization_id)
                .eq("worksheet_id", worksheet_id)
                .eq("success", True)
                .in_("image_column_id", target_column_ids)
                .in_("row_id", chunk)
                .range(offset, offset + _PRIOR_LOG_PAGE_SIZE - 1)
                .execute()
            )
            page = result.data or []
            for entry in page:
                rid = int(entry["row_id"])
                cid = int(entry["image_column_id"])
                fname = entry.get("image_filename") or ""
                if fname:
                    out.setdefault(rid, []).append((cid, fname))

            if len(page) < _PRIOR_LOG_PAGE_SIZE:
                break
            offset += _PRIOR_LOG_PAGE_SIZE

    return out


def _fetch_prior_processed(
    organization_id: str,
    worksheet_id: int,
    target_column_ids: List[int],
    row_ids: List[int],
) -> Dict[int, List[Tuple[int, str]]]:
    """
    Return {row_id: [(col_id, filename), ...]} for prior successful conversions
    on this worksheet, filtered to the config's target columns and the given rows.

    Used by preflight_backfill_budget and ImageProcessor._build_synthetic_states_for_backfill
    to construct synthetic RowState objects so filter_attachments produces a precise
    image count and skips already-processed signatures.

    Retries up to 3 times with exponential backoff on transient errors. Raises
    PriorLogUnavailable after the final attempt — callers MUST NOT treat the
    failure as "no prior history", because that path causes overcharge by
    re-billing already-processed images. The customer-facing fix is for the
    backfill caller to abort with a clear retryable error.
    """
    if not target_column_ids or not row_ids:
        return {}

    last_error: Optional[BaseException] = None
    delay = _PRIOR_LOG_RETRY_BASE_DELAY_S
    for attempt in range(_PRIOR_LOG_RETRY_ATTEMPTS):
        try:
            return _fetch_prior_processed_inner(
                organization_id, worksheet_id, target_column_ids, row_ids
            )
        except Exception as e:
            last_error = e
            if attempt < _PRIOR_LOG_RETRY_ATTEMPTS - 1:
                jitter = random.uniform(0, delay * 0.3)
                log.warning(
                    "i2c_bolt_gate: prior-processed lookup failed org=%s ws=%s "
                    "(attempt %d/%d): %s — retrying in %.1fs",
                    organization_id,
                    worksheet_id,
                    attempt + 1,
                    _PRIOR_LOG_RETRY_ATTEMPTS,
                    e,
                    delay + jitter,
                )
                time.sleep(delay + jitter)
                delay *= 2
            else:
                log.error(
                    "i2c_bolt_gate: prior-processed lookup failed org=%s ws=%s "
                    "after %d attempts: %s",
                    organization_id,
                    worksheet_id,
                    _PRIOR_LOG_RETRY_ATTEMPTS,
                    e,
                )

    raise PriorLogUnavailable(
        f"imagetocell_usage_log query failed after {_PRIOR_LOG_RETRY_ATTEMPTS} "
        f"attempts (org={organization_id}, ws={worksheet_id}): {last_error}"
    ) from last_error


def compute_backfill_estimate(
    organization_id: str,
    config: Any,
    rows_with_attachments: List[Dict[str, Any]],
    sheet_service: Any,
) -> Optional[Dict[str, Any]]:
    """
    Full backfill consumption estimate for one config.

    Returns the breakdown of images and bolts the backfill would consume, plus
    whether the org's hard ceiling would be exceeded. Unlike
    ``preflight_backfill_budget`` (which only surfaces the breakdown when it
    would block), this function ALWAYS returns the full estimate so consumers
    — the UI preview endpoint in particular — can show the user what they are
    about to pay before they decide to run the job. SH-121.

    Enforcement context: a job is reported as ``blocked`` only when the org has
    a hard ceiling (Free plans or paid plans with ``hard_cap_percentage``) AND
    the projected images exceed the affordable count. Paid plans without a hard
    cap are never blocked — overage is permitted and billed — but the estimate
    still reports the overage so the UI can present it.

    Args:
        organization_id: Organization whose budget is being estimated.
        config: ImageToCellConfig about to be backfilled. Used to determine
            mode-specific per-row image counts.
        rows_with_attachments: Rows already fetched via
            ``SheetService.get_rows_with_attachments``.
        sheet_service: SheetService used to extract image attachments per row.

    Returns:
        ``None`` when the estimate cannot be produced — gate disabled, usage
        context unavailable, or the estimator crashes. Callers must NOT fabricate
        numbers in that case (the preview endpoint should respond with an
        explicit "estimate unavailable" instead of a misleading zero).

        Otherwise a dict with:
            - ``images_needed`` (int) — images the backfill would write
            - ``images_available`` (int) — affordable image count under the
              remaining effective budget
            - ``bolts_per_image`` (int)
            - ``bolts_needed`` (int) — images_needed * bolts_per_image
            - ``bolts_available_in_cap`` (int) — remaining effective bolts
            - ``bolts_overage_estimated`` (int) — max(0, needed - available)
            - ``has_ceiling`` (bool) — True for Free / hard-cap plans
            - ``blocked`` (bool) — True only when has_ceiling AND over budget
            - ``block_reason`` (str | None) — ``"backfill_budget_insufficient"``
              or ``"prior_log_unavailable"`` when blocked
            - ``error`` (str | None) — human-readable detail for
              ``prior_log_unavailable``
    """
    if not i2c_bolt_gate_enabled():
        return None

    ctx = load_i2c_org_usage_context(organization_id)
    if ctx is None:
        log.warning(
            "i2c_bolt_gate: no context for org=%s — estimate unavailable",
            organization_id,
        )
        return None

    bolts_per_image = int(ctx.get("bolts_per_image") or 1)
    if bolts_per_image < 1:
        bolts_per_image = 1

    remaining_eff = int(ctx.get("remaining_effective_bolts") or 0)
    images_available = remaining_eff // bolts_per_image
    has_ceiling = ctx.get("ceiling_bolts") is not None

    # Lazy import to avoid any module-load-time coupling with core/modes.
    from src.core.modes.default import DefaultMode
    from src.core.modes.name_map import NameMapMode
    from src.core.modes.replace import ReplaceMode
    from src.models.row_state import RowState
    # SH-145: the cell-content tier check (the same one RowProcessor uses
    # to skip overwriting manually-uploaded images) needs to run in the
    # estimator too. Without this the preview overcounts on sheets where
    # files appear both as attachments AND already pasted into the target
    # cells — the runtime would skip those cells, but the estimator would
    # count them. RowProcessor exposes the two pieces as @staticmethod, so
    # we can reuse them without instantiating a processor.
    from src.core.row_processor import RowProcessor

    modes = {
        "default": DefaultMode(),
        "replace": ReplaceMode(),
        "name map": NameMapMode(),
    }

    config_type_value = getattr(getattr(config, "config_type", None), "value", "default")
    mode_key = (config_type_value or "default").lower()
    mode = modes.get(mode_key, modes["default"])

    # Fetch prior processing history for default, replace, and name_map modes.
    # If the lookup fails after retries we surface a blocked estimate with
    # ``prior_log_unavailable`` rather than fabricating "no prior history",
    # which would re-bill already-processed images.
    prior_log: Dict[int, List[Tuple[int, str]]] = {}
    image_cols: List[int] = []
    if mode_key in ("default", "replace", "name map"):
        target_col_ids = [c for c in (getattr(config, "image_columns", None) or []) if c]
        if mode_key == "default":
            image_cols = list(getattr(config, "image_columns", None) or [])
        if target_col_ids:
            all_row_ids = [int(row.get("id") or 0) for row in rows_with_attachments]
            try:
                prior_log = _fetch_prior_processed(
                    organization_id, config.worksheet_id, target_col_ids, all_row_ids
                )
            except PriorLogUnavailable as e:
                log.error(
                    "i2c_bolt_gate: estimate aborted org=%s config=%s — "
                    "prior log unavailable: %s",
                    organization_id,
                    getattr(config, "id", None),
                    e,
                )
                # prior_log_unavailable means we cannot trust the estimator at
                # all — anything we report would be misleading. Surface zeros
                # for the count/availability fields so the UI shows an explicit
                # "retry" state rather than a confident-looking number, and
                # preserve the pre-refactor wrapper contract that callers
                # already rely on.
                return {
                    "images_needed": 0,
                    "images_available": 0,
                    "bolts_per_image": bolts_per_image,
                    "bolts_needed": 0,
                    "bolts_available_in_cap": 0,
                    "bolts_overage_estimated": 0,
                    "has_ceiling": has_ceiling,
                    "blocked": True,
                    "block_reason": "prior_log_unavailable",
                    "error": str(e),
                }

    images_needed = 0
    try:
        for row in rows_with_attachments:
            row_id = row.get("id") or 0
            atts = sheet_service.extract_row_attachments(row, images_only=True)
            if not atts:
                continue

            # SH-145: snapshot the cells of this row that already hold an
            # image (manual upload, prior run, anything Smartsheet returns
            # with cell.image set). Used downstream to apply the same
            # tier-skip logic the runtime applies in RowProcessor — without
            # this, the estimator would count attachments that the runtime
            # would skip, and mixed-upload sheets get an inflated preview.
            cell_images = RowProcessor._extract_cell_images(row, config)
            columns_with_images = set(cell_images.keys())

            state = RowState(config_id=config.id, row_id=row_id)

            # SH-108: drop prior-log entries whose target cell is empty —
            # same filter as ImageProcessor._build_synthetic_states_for_backfill
            # so estimate and runtime agree on "already done" work.
            prior_entries = RowProcessor.filter_prior_for_filled_cells(
                list(prior_log.get(row_id) or []),
                row,
                row_id=row_id,
            )

            # Replace mode: filter_attachments now respects state.last_ids
            # (post-1.2.0), but in the estimator we pass an empty state and
            # subtract prior_log filenames explicitly. Both approaches give
            # the same answer; the explicit-subtraction path is preserved
            # so the contract stays observable in logs.
            if mode_key == "replace" and prior_entries:
                processed_filenames = {fname for _, fname in prior_entries}
                newest_n = mode.filter_attachments(atts, state, config)
                eligible = [
                    att for att in newest_n if att.name not in processed_filenames
                ]
            else:
                # Default / name_map: build synthetic state from prior history.
                # name_map uses last_ids (not locked_columns), so the same
                # approach works — lock_column calls silently fail (image_cols
                # is empty for name_map) but last_ids is still populated, which
                # is what filter_attachments checks.
                if mode_key in ("default", "name map") and prior_entries:
                    att_by_name = {a.name: a for a in atts}
                    for col_id, filename in prior_entries:
                        att = att_by_name.get(filename)
                        if att is None:
                            continue
                        state.last_ids.add(att.signature)
                        if att.created_timestamp > state.last_ts:
                            state.last_ts = att.created_timestamp
                        try:
                            col_idx = image_cols.index(col_id)
                            state.lock_column(col_idx)
                        except ValueError:
                            pass

                if not mode.should_process_row(atts, state, config):
                    continue
                eligible = list(mode.filter_attachments(atts, state, config))

            # SH-145: apply the same SH-105 tier check the runtime applies.
            # Pairs each surviving attachment with its target column via the
            # mode's get_target_columns mapping, then drops pairs that would
            # overwrite an existing cell image. is_scan_context=True because
            # the estimator is always previewing a backfill — never a live
            # webhook — so the replace+webhook overwrite branch (T3) does
            # not apply here.
            if eligible and cell_images:
                column_mapping = mode.get_target_columns(
                    eligible, state, config, columns_with_images=columns_with_images
                )
                survivors = RowProcessor._apply_existing_image_tiers(
                    eligible,
                    column_mapping,
                    cell_images,
                    mode_key,
                    True,   # is_scan_context
                    row_id,
                )
                eligible = [att for att, _ in survivors]

            images_needed += len(eligible)
    except Exception as e:
        # Estimator crash → return None so the wrapper / endpoint surfaces an
        # explicit "estimate unavailable" instead of fabricating zeros.
        log.warning(
            "i2c_bolt_gate: backfill estimator failed org=%s config=%s err=%s — "
            "returning None (caller surfaces 'estimate unavailable')",
            organization_id,
            getattr(config, "id", None),
            e,
        )
        return None

    bolts_needed = images_needed * bolts_per_image
    bolts_available_in_cap = remaining_eff
    bolts_overage_estimated = max(0, bolts_needed - bolts_available_in_cap)
    blocked = has_ceiling and images_needed > images_available
    block_reason = "backfill_budget_insufficient" if blocked else None

    log.info(
        "i2c_bolt_gate: estimate org=%s config=%s mode=%s images_needed=%s "
        "available=%s bolts_needed=%s overage=%s ceiling=%s blocked=%s",
        organization_id,
        getattr(config, "id", None),
        mode_key,
        images_needed,
        images_available,
        bolts_needed,
        bolts_overage_estimated,
        has_ceiling,
        blocked,
    )

    return {
        "images_needed": images_needed,
        "images_available": images_available,
        "bolts_per_image": bolts_per_image,
        "bolts_needed": bolts_needed,
        "bolts_available_in_cap": bolts_available_in_cap,
        "bolts_overage_estimated": bolts_overage_estimated,
        "has_ceiling": has_ceiling,
        "blocked": blocked,
        "block_reason": block_reason,
    }


def preflight_backfill_budget(
    organization_id: str,
    config: Any,
    rows_with_attachments: List[Dict[str, Any]],
    sheet_service: Any,
) -> Optional[Dict[str, Any]]:
    """
    Block-decision wrapper over ``compute_backfill_estimate`` (SH-121 refactor).

    Preserves the original contract: returns ``None`` when the backfill may
    proceed, or a blocking dict
    ``{reason, images_needed, images_available, bolts_per_image[, error]}`` when
    the backfill would exceed the org's hard ceiling. The actual budget
    calculation lives in ``compute_backfill_estimate`` so the same logic is
    reused by the preview endpoint without duplication.

    Performance note: paid plans without a hard ceiling short-circuit here
    (gate / context / ceiling checks only) before invoking the full estimator,
    matching the original function's behaviour.

    Returns:
        ``None`` when:
            - the gate is disabled
            - the org has no hard ceiling (overage permitted)
            - the estimator returned a non-blocked estimate

        Blocking dict when the Hub budget cannot be loaded (``reason=
        budget_unavailable``), when the estimator cannot produce a number
        (``reason=estimate_unavailable``), or when the backfill would exceed
        the org's hard ceiling — fail-closed in all three cases.
    """
    if not i2c_bolt_gate_enabled():
        return None

    ctx = load_i2c_org_usage_context(organization_id)
    if ctx is None:
        log.error(
            "i2c_bolt_gate: no context for org=%s — refusing backfill (fail-closed)",
            organization_id,
        )
        return {
            "reason": "budget_unavailable",
            "images_needed": 0,
            "images_available": 0,
            "bolts_per_image": 1,
        }

    # Only enforce when there is a hard ceiling. Paid plans without a hard cap
    # permit overage and bill accordingly — short-circuit before the full
    # estimator runs to preserve the original performance characteristics on
    # large worksheets.
    if ctx.get("ceiling_bolts") is None:
        return None

    estimate = compute_backfill_estimate(
        organization_id, config, rows_with_attachments, sheet_service
    )
    if estimate is None:
        # Estimator failed → fail-closed (same shape as budget_unavailable).
        # Never authorize a hard-cap backfill when we cannot price it.
        log.error(
            "i2c_bolt_gate: backfill estimate unavailable org=%s config=%s — "
            "refusing backfill (fail-closed)",
            organization_id,
            getattr(config, "id", None),
        )
        return {
            "reason": "estimate_unavailable",
            "images_needed": 0,
            "images_available": 0,
            "bolts_per_image": int(ctx.get("bolts_per_image") or 1),
        }

    if not estimate.get("blocked"):
        bolts_per_image = int(estimate.get("bolts_per_image") or 1)
        log.info(
            "i2c_bolt_gate: backfill budget OK org=%s config=%s needed=%s "
            "available=%s bolts_per_image=%s",
            organization_id,
            getattr(config, "id", None),
            estimate.get("images_needed"),
            estimate.get("images_available"),
            bolts_per_image,
        )
        return None

    log.warning(
        "i2c_bolt_gate: backfill budget insufficient org=%s config=%s "
        "needed=%s available=%s bolts_per_image=%s — backfill will be skipped "
        "(config remains active)",
        organization_id,
        getattr(config, "id", None),
        estimate.get("images_needed"),
        estimate.get("images_available"),
        estimate.get("bolts_per_image"),
    )
    blocking: Dict[str, Any] = {
        "reason": estimate.get("block_reason") or "backfill_budget_insufficient",
        "images_needed": int(estimate.get("images_needed") or 0),
        "images_available": int(estimate.get("images_available") or 0),
        "bolts_per_image": int(estimate.get("bolts_per_image") or 1),
    }
    if estimate.get("error"):
        blocking["error"] = estimate["error"]
    return blocking
