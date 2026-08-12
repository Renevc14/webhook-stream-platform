"""
Row Processor
=============

Handles processing of individual rows.
"""

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID


# SH-105 hash-suffix matcher. i2c-3 renames attachments during the
# `/cellimages` upload via TempFileManager.create_temp_file
# (`f"{base}_{uuid.uuid4().hex[:8]}{ext}"`), so the altText that ends up
# on the cell is `dog.2_b3c0dc04.jpg` even though the source attachment
# is `dog.2.jpg`. SH-105 / SH-108 compare attachment.name against
# cell.image.altText to detect "same file"; without this strip, a sheet
# pre-populated by an earlier backfill (template, cloned sheet, prior
# run) is treated as "different image" by the matcher and gets
# re-processed + re-billed.
_HASH_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}(\.[^.]+)$", re.IGNORECASE)


def _strip_hash_suffix(name: Optional[str]) -> Optional[str]:
    """Return `name` with i2c-3's `_<8 hex>` upload suffix removed.

    Idempotent: filenames that never had a suffix (manual uploads via the
    Smartsheet UI, drag-drop attachments) are returned unchanged.
    """
    if not name:
        return name
    return _HASH_SUFFIX_RE.sub(r"\1", name)

from src.models.config import ImageToCellConfig, ConfigType
from src.models.attachment import Attachment
from src.models.row_state import RowState
from src.models.results import RowResult, ImageResult
# SH-102: RowProcessor no longer carries a reference to the legacy
# `SmartsheetService`; the smartsheet param exists only as an optional
# slot for the small number of tests that still hand in a MagicMock.
from src.services.smartsheet.sheets import SheetService
from src.services.supabase.state_repo import StateRepository
from src.core.image_processor import ImageHandler
from src.core.modes.base import BaseMode
from src.core.modes.default import DefaultMode
from src.core.modes.replace import ReplaceMode
from src.core.modes.name_map import NameMapMode
from src.utils.image_utils import TempFileManager
from src.config.settings import Settings
from src.services.usage_log import ALREADY_LOGGED
from src.services.usage_log import log_image_conversion as log_usage
from src.services.stream_events import publish_image_processed

try:
    from src.services.bolt_cap import get_remaining_image_capacity
except ImportError:
    get_remaining_image_capacity = None

log = logging.getLogger(__name__)


class RowProcessor:
    """
    Processes individual rows.

    Responsible for:
    - Determining which attachments to process
    - Coordinating image processing
    - Updating row state
    """

    # Mode registry — built per instance so name_map picks up settings.
    _mode_classes = {
        "default": DefaultMode,
        "replace": ReplaceMode,
    }

    def __init__(
        self,
        smartsheet: Any,
        state_repo: StateRepository,
        settings: Settings,
        organization_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        billing: Any = None,
    ):
        """
        Initialize row processor.

        Args:
            smartsheet: Legacy parameter kept only as a slot for tests
                that hand in a MagicMock. SH-102 removed the in-tree
                ``SmartsheetService`` path; production callers may now
                pass ``None``.
            state_repo: State repository.
            settings: Application settings.
            organization_id: SH-100 — scopes the ``ImageHandler`` /
                ``AttachmentService`` it builds to the right org for the
                new ``SmartsheetClient`` (encrypted token cache +
                cluster-wide rate limit). Optional only for tests that
                mock ``ImageHandler`` outright.
            billing: Optional ``BillingPort`` (tests inject ``MockBilling``;
                production resolves the lifespan singleton via
                ``get_billing``).
        """
        self._ss = smartsheet
        self._state_repo = state_repo
        self._settings = settings
        self._billing = billing
        # SH-100: ``extract_row_attachments`` is a sync ``@staticmethod``,
        # so the SheetService instance never actually touches the
        # ``smartsheet`` reference — it exists only so existing tests can
        # swap ``rp._sheet_service`` for a MagicMock.
        self._sheet_service = SheetService(smartsheet)  # type: ignore[arg-type]
        self._image_handler = ImageHandler(organization_id, settings, owner_user_id)
        try:
            max_name_map_attempts = int(
                getattr(settings, "name_map_max_upload_attempts", 5) or 5
            )
        except (TypeError, ValueError):
            max_name_map_attempts = 5
        self._modes: Dict[str, BaseMode] = {
            **{name: cls() for name, cls in self._mode_classes.items()},
            "name map": NameMapMode(max_upload_attempts=max_name_map_attempts),
        }

    async def _resolve_billing(self) -> Any:
        if self._billing is not None:
            return self._billing
        try:
            from src.adapters.billing.port import get_billing

            return await get_billing(self._settings)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Billing port unavailable (%s) — falling back to legacy capacity peek",
                exc,
            )
            return None

    async def process(
        self,
        config: ImageToCellConfig,
        row: Dict[str, Any],
        state: Optional[RowState] = None,
        skip_usage_logging: bool = False,
        is_scan_context: bool = True,
    ) -> RowResult:
        """
        Process a single row.

        Args:
            config: Configuration
            row: Row data from Smartsheet
            state: Optional existing state (fetched if not provided)
            skip_usage_logging: If True, the caller is responsible for
                writing usage_log entries (DLQ retries).
            is_scan_context: True for backfill / polling scans, False for
                live webhook events. SH-105 uses this to decide whether
                replace mode should overwrite a cell that already holds a
                different image (webhook=yes, scan=no — defensive).

        Returns:
            RowResult with processing outcome
        """
        start_time = time.time()
        row_id = row["id"]
        # SH-181 / billing port: bolts reserved via Redis before image work.
        # Refunds use Reservation.bolts (exact hold) — never reprice.
        reserved_images = 0
        reserved_bolts = 0
        operation_id: Optional[str] = None
        org_uuid: Optional[UUID] = None

        try:
            # Get or create state
            if state is None:
                state = self._state_repo.get(config.id, row_id)
                if state is None:
                    state = RowState(config_id=config.id, row_id=row_id)

            # Get processing mode
            mode = self._get_mode(config.config_type)

            # SH-100: extract_row_attachments is a pure @staticmethod, so
            # ``self._sheet_service.extract_row_attachments`` and
            # ``SheetService.extract_row_attachments`` behave identically.
            # Using the instance attribute keeps existing test mocks working
            # (they replace ``rp._sheet_service`` to control the return value).
            attachments = self._sheet_service.extract_row_attachments(
                row, images_only=True
            )

            # SH-105: snapshot which config columns already hold an image in
            # the live row (manual uploads or prior runs). The map stores the
            # full image dict so we can compare altText for filename-match
            # downstream in the tier decision.
            cell_images = self._extract_cell_images(row, config)
            columns_with_images = set(cell_images.keys())

            # SH-108 (live path): prune state entries whose backing cell is
            # currently empty. Persisted RowState outlives manual cell
            # clearings, so without this default.should_process_row sees the
            # row as "full" (last_ids count >= n_slots) and default routing
            # excludes already-locked columns even when the cells were wiped.
            # The synthetic-state builder runs the same logic at backfill
            # build time; this block extends the coverage to webhook events
            # and to backfill rows that DO have a row_state entry.
            #
            # Safety: only runs when row payload carries cells (have_cells
            # check), to avoid wiping state from a partial response.
            if row.get("cells") and (state.locked_columns or state.last_ids):
                self._prune_stale_state(
                    state, attachments, cell_images, columns_with_images, config, row_id
                )

            # SH-111: snapshot state.last_ids AFTER prune. Used later to
            # distinguish "newly processed" attachments (signature was NOT
            # in state when this run started) from "shuffled" ones
            # (signature was already there — the attachment is being
            # re-uploaded to a different column to preserve the replace
            # mode "newest N in N columns" invariant). Shuffled attachments
            # bypass usage_log so the customer is only billed once per
            # attachment regardless of how many times it moves columns.
            pre_run_last_ids = set(state.last_ids)

            # Check if row should be processed
            if not mode.should_process_row(attachments, state, config):
                return RowResult(
                    success=True,
                    row_id=row_id,
                    images_skipped=len(attachments),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # Filter attachments per mode rules
            to_process = mode.filter_attachments(attachments, state, config)

            # SH-105: in default mode, drop attachments whose filename already
            # appears as the altText of any cell in this config. Prevents
            # duplicating the same manual image into a second column when the
            # user has "next available" slots open. Smartsheet sets altText to
            # the original filename when an image is inserted into a cell.
            # altTexts are normalised with _strip_hash_suffix so a cell that
            # was populated by a previous i2c-3 run (altText carries i2c-3's
            # `_<8 hex>` suffix) still matches the source attachment's name.
            if mode.name == "default" and cell_images:
                existing_alts = {
                    _strip_hash_suffix(img.get("altText"))
                    for img in cell_images.values()
                    if isinstance(img, dict) and img.get("altText")
                }
                if existing_alts:
                    before_count = len(to_process)
                    dropped = [a.name for a in to_process if a.name in existing_alts]
                    to_process = [a for a in to_process if a.name not in existing_alts]
                    if dropped:
                        log.info(
                            "[SH-105:anti-dup] row=%s dropped %d/%d attachment(s) %s — "
                            "filename already present as altText in this config",
                            row_id, len(dropped), before_count, dropped,
                        )

            column_mapping = mode.get_target_columns(
                to_process, state, config, columns_with_images=columns_with_images
            )

            # SH-105 diagnostic: log when get_target_columns returns no slots
            # for default mode. The tier loop below will silently skip every
            # attachment in this case (col_id is None) — without this log the
            # caller sees a generic "No new images to process" with no reason.
            if mode.name == "default" and to_process and not column_mapping:
                log.info(
                    "[SH-105:no-slot] row=%s skip — default mode has no available "
                    "columns (filled=%s, locked=%s, config=%s)",
                    row_id,
                    sorted(columns_with_images),
                    list(state.locked_columns),
                    [c for c in (config.image_columns or []) if c],
                )

            # SH-105: tiered decision per (attachment, target_col).
            # Drops pairs that would overwrite a manual upload or duplicate
            # an image already in the row. Survivors keep their original
            # ordering and get re-indexed so column_mapping stays aligned.
            survivors = self._apply_existing_image_tiers(
                to_process,
                column_mapping,
                cell_images,
                mode.name,
                is_scan_context,
                row_id,
            )
            to_process = [att for att, _ in survivors]
            column_mapping = {i: col_id for i, (_, col_id) in enumerate(survivors)}

            if not to_process:
                return RowResult(
                    success=True,
                    row_id=row_id,
                    images_skipped=len(attachments),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # MID-JOB BOLT CAP: shared BillingPort (smarthub_core.billing).
            # Holds exact bolts under operation_id; release never reprices.
            billing = await self._resolve_billing()
            if billing is not None:
                try:
                    org_uuid = UUID(str(config.organization_id))
                except (TypeError, ValueError):
                    log.error(
                        "Row %s: invalid organization_id=%r — skipping image work",
                        row_id,
                        config.organization_id,
                    )
                    return RowResult(
                        success=True,
                        row_id=row_id,
                        images_skipped=len(attachments),
                        processing_time_ms=int((time.time() - start_time) * 1000),
                    )

                # Stable per attempt: config+row+attachment set + attempt uuid
                # so retries do not collide on the bolt-hold key incorrectly.
                att_sig = ",".join(
                    sorted(str(getattr(a, "signature", a.id)) for a in to_process)
                )
                batch_token = f"{hash(att_sig) & 0xFFFFFFFF:08x}:{uuid.uuid4().hex[:12]}"
                operation_id = f"{config.id}:{row_id}:{batch_token}"

                n = len(to_process)
                reservation = await billing.reserve(
                    org_uuid, n, operation_id=operation_id
                )
                allowed = reservation.allowed
                if allowed:
                    reserved_images = n
                    reserved_bolts = int(reservation.bolts or 0)
                else:
                    # Halve units until budget fits, then skip the row.
                    n = len(to_process) // 2
                    while n > 0 and not allowed:
                        batch_token = (
                            f"{hash(att_sig) & 0xFFFFFFFF:08x}:{uuid.uuid4().hex[:12]}"
                        )
                        operation_id = f"{config.id}:{row_id}:{batch_token}"
                        reservation = await billing.reserve(
                            org_uuid, n, operation_id=operation_id
                        )
                        allowed = reservation.allowed
                        if allowed:
                            to_process = to_process[:n]
                            column_mapping = {
                                k: v for k, v in column_mapping.items() if k < n
                            }
                            reserved_images = n
                            reserved_bolts = int(reservation.bolts or 0)
                            log.info(
                                "Row %s: Limiting to %s images (bolt reserve) "
                                "of %s requested",
                                row_id,
                                n,
                                len(attachments),
                            )
                            break
                        n //= 2

                if not allowed:
                    cap_reason = reservation.reason if reservation else None
                    if not cap_reason and get_remaining_image_capacity:
                        _, cap_reason = get_remaining_image_capacity(
                            config.organization_id
                        )
                    log.info(
                        "Row %s: Stopping - %s, skipping image processing",
                        row_id,
                        cap_reason or "bolt cap reached",
                    )
                    return RowResult(
                        success=True,
                        row_id=row_id,
                        images_skipped=len(attachments),
                        processing_time_ms=int((time.time() - start_time) * 1000),
                    )
            elif get_remaining_image_capacity:
                # Legacy peek — only when billing could not be resolved
                # (unit tests without a BillingPort / broken settings).
                remaining, cap_reason = get_remaining_image_capacity(
                    config.organization_id
                )
                if remaining <= 0:
                    log.info(
                        f"Row {row_id}: Stopping - {cap_reason or 'bolt cap reached'}, "
                        "skipping image processing"
                    )
                    return RowResult(
                        success=True,
                        row_id=row_id,
                        images_skipped=len(attachments),
                        processing_time_ms=int((time.time() - start_time) * 1000),
                    )
                if remaining < len(to_process):
                    to_process = to_process[:remaining]
                    column_mapping = {
                        k: v for k, v in column_mapping.items() if k < remaining
                    }
                    log.info(
                        f"Row {row_id}: Limiting to {remaining} images (cap) of "
                        f"{len(attachments)} requested"
                    )

            # Clear cells if needed (replace mode)
            if hasattr(mode, 'should_clear_cells') and mode.should_clear_cells():
                self._clear_target_cells(config, row_id, column_mapping)

            # Process images (SH-101: async)
            image_results = await self._process_images(
                config,
                row_id,
                to_process,
                column_mapping,
            )

            # B15: write usage log BEFORE updating state_repo. The two
            # writes can fail independently and we cannot wrap them in a
            # transaction across services. Choosing the order matters:
            #
            #   - Old order (state then log): if state.save succeeded but
            #     log_usage failed, we'd have state but no billing entry —
            #     subcobro.
            #   - Newer concern: if state.save FAILED but the upload
            #     succeeded, the next backfill saw no state, no usage_log,
            #     and re-uploaded + re-billed. This was the customer-
            #     reported double-bill on transient Supabase failures.
            #
            # Inverting the order eliminates the double-bill: if log_usage
            # succeeds but state.save fails, the next backfill's synthetic
            # state build sees the usage_log entry and skips the row →
            # 0 bolts charged again. The remaining failure mode (log fails
            # AFTER upload) leaves the cell filled but no charge — a
            # subcobro, accepted as the lesser evil compared to overcharge.
            #
            # Skipped during DLQ retries — the DLQ processor handles its
            # own logging to avoid double-counting failures that were
            # already recorded.
            #
            # Billing policy: charge for every successful image write
            # (including reprocess after clear). Redis is reserved before
            # upload; holds stick for successful work. Refund only for
            # failures, SH-111 shuffles (intentional free relocate), and
            # transient usage_log failures (prefer subcobro over phantoms).
            #
            # Do NOT refund solely because usage_log returned ALREADY_LOGGED
            # (legacy SH-98 unique index). That made cleared-cell re-backfill
            # net $0 in Redis while work was done. Migration
            # 20260803190000 drops that unique index so Hub/Postgres can
            # insert an additional success row and stay aligned with Redis
            # via get_org_billing_usage_total.
            newly_billed = 0
            nobill_by_reason: Dict[str, int] = {}
            if not skip_usage_logging:
                user_id = getattr(config, "created_by", None) or getattr(config, "user_id", None)
                for i, (att, res) in enumerate(zip(to_process, image_results)):
                    col_id = column_mapping.get(i) or res.column_id
                    col_name = config.get_column_name(col_id) if col_id else None
                    # SH-111: skip usage_log (and therefore billing) for
                    # attachments that were already in state.last_ids before
                    # this run started. They were processed before and are
                    # only being moved to a different column to preserve the
                    # newest-N-in-N-cols invariant of replace mode. The
                    # upload to Smartsheet still happens (the cell needs the
                    # image) but no new usage_log row is created, so the
                    # customer is billed once per attachment, never per
                    # column shuffle.
                    if res.success and att.signature in pre_run_last_ids:
                        log.info(
                            "[SH-111:shuffle] row=%s att=%r relocated to col=%s — no billing (already counted on first processing)",
                            row_id, att.name, col_id,
                        )
                        nobill_by_reason["shuffle_no_bill"] = (
                            nobill_by_reason.get("shuffle_no_bill", 0) + 1
                        )
                        continue
                    usage_id = log_usage(
                        organization_id=config.organization_id,
                        user_id=user_id,
                        config_id=config.id,
                        config_name=config.configuration_name,
                        config_type=config.config_type.value,
                        worksheet_id=config.worksheet_id,
                        worksheet_name=config.worksheet_name,
                        row_id=row_id,
                        image_column_id=col_id or 0,
                        image_column_name=col_name,
                        image_filename=res.filename,
                        success=res.success,
                        image_size_bytes=res.original_size_bytes or None,
                        processing_duration_ms=res.processing_time_ms or None,
                        error_message=res.error,
                        is_system_generated=False,
                    )
                    # SH-155: emit image_processed to the platform bus for
                    # every successfully uploaded image. Mirrors billing
                    # semantics (SH-111 shuffles skipped above; DLQ retries
                    # publish from the DLQ processor alongside their own
                    # usage log). Best-effort — never fails the row.
                    if res.success:
                        if usage_id == ALREADY_LOGGED:
                            # Successful write still billable. Keep Redis
                            # hold. Warn: without migration 20260803190000
                            # Postgres has no new row → Hub reseed drift.
                            newly_billed += 1
                            log.warning(
                                "Row %s: usage_log duplicate after successful "
                                "upload (col=%s file=%s) — keeping bolt charge; "
                                "apply migration 20260803190000 so Hub records "
                                "reprocess rows",
                                row_id,
                                col_id,
                                res.filename,
                            )
                            await publish_image_processed(
                                org_id=config.organization_id,
                                config_id=config.id,
                                worksheet_id=config.worksheet_id,
                                row_id=row_id,
                                attachment_id=att.id,
                                column_id=col_id,
                                filename=res.filename,
                            )
                        elif usage_id:
                            newly_billed += 1
                            await publish_image_processed(
                                org_id=config.organization_id,
                                config_id=config.id,
                                worksheet_id=config.worksheet_id,
                                row_id=row_id,
                                attachment_id=att.id,
                                column_id=col_id,
                                filename=res.filename,
                            )
                        else:
                            # Transient RPC / empty payload — prefer
                            # subcobro (refund Redis) over phantom holds.
                            nobill_by_reason["usage_log_not_billed"] = (
                                nobill_by_reason.get("usage_log_not_billed", 0) + 1
                            )

            # SH-181 + no-bill: release exact bolts that should not stick.
            # Never reprice via n_images * bolts_per_image.
            # DLQ (skip_usage_logging): keep successes for the caller;
            # refund failures only. Successful uploads (including legacy
            # ALREADY_LOGGED) are not refunded.
            if reserved_images and reserved_bolts > 0 and org_uuid is not None:
                if skip_usage_logging:
                    refund_images = sum(1 for r in image_results if not r.success)
                    refund_reason = "image_failed"
                else:
                    refund_images = reserved_images - newly_billed
                    if nobill_by_reason:
                        refund_reason = ",".join(
                            f"{k}={v}" for k, v in sorted(nobill_by_reason.items())
                        )
                        failed_n = sum(1 for r in image_results if not r.success)
                        if failed_n:
                            refund_reason = f"image_failed={failed_n},{refund_reason}"
                    else:
                        refund_reason = "image_failed"
                if (
                    refund_images > 0
                    and billing is not None
                ):
                    refund = (reserved_bolts * refund_images) // reserved_images
                    if refund > 0:
                        log.info(
                            "Row %s: Refunding %s bolts (images=%s/%s) "
                            "reason=%s operation_id=%s",
                            row_id,
                            refund,
                            refund_images,
                            reserved_images,
                            refund_reason,
                            operation_id,
                        )
                        await billing.release(
                            org_uuid,
                            amount=refund_images,
                            bolts=refund,
                            operation_id=operation_id,
                        )

            # Update state — runs after usage log so a transient state.save
            # failure does NOT cause the next backfill to re-bill the row.
            state_changed = False
            processed_atts = [
                to_process[i] for i, r in enumerate(image_results)
                if r.success
            ]
            if processed_atts:
                state = mode.update_state_after_processing(
                    state,
                    processed_atts,
                    column_mapping,
                    config,
                )
                state_changed = True

            # SH-176: name_map saves failed_attempts even when every upload
            # fails so rows do not retry forever and partial failures are not
            # abandoned when a sibling column succeeds.
            if isinstance(mode, NameMapMode) and image_results:
                state = mode.record_processing_outcomes(
                    state,
                    to_process,
                    image_results,
                )
                state_changed = True

            if state_changed:
                self._state_repo.save(state)

            # Build result
            processing_time = int((time.time() - start_time) * 1000)

            return RowResult(
                success=any(r.success for r in image_results),
                row_id=row_id,
                images_processed=sum(1 for r in image_results if r.success),
                images_failed=sum(1 for r in image_results if not r.success),
                images_skipped=len(attachments) - len(to_process),
                image_results=image_results,
                processing_time_ms=processing_time,
            )

        except Exception as e:
            # SH-181: row blew up after reserve — refund the exact hold so
            # Redis does not retain phantom consumption across DLQ retries.
            if reserved_bolts > 0 and org_uuid is not None:
                try:
                    billing = self._billing
                    if billing is None:
                        billing = await self._resolve_billing()
                    if billing is not None:
                        await billing.release(
                            org_uuid,
                            bolts=reserved_bolts,
                            operation_id=operation_id,
                        )
                except Exception as release_err:
                    log.warning(
                        "Row %s: bolt release after failure also failed: %s",
                        row_id,
                        release_err,
                    )
            log.error(f"Failed to process row {row_id}: {e}")
            return RowResult(
                success=False,
                row_id=row_id,
                error=str(e),
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    def _get_mode(self, config_type: ConfigType) -> BaseMode:
        """Get the processing mode for a config type."""
        mode_name = config_type.value.lower()
        mode = self._modes.get(mode_name)

        if mode is None:
            log.warning(f"Unknown config type: {config_type}, using default")
            mode = self._modes["default"]

        return mode

    async def _process_images(
        self,
        config: ImageToCellConfig,
        row_id: int,
        attachments: List[Attachment],
        column_mapping: Dict[int, int],
    ) -> List[ImageResult]:
        """Process images with temp file management (SH-101: async)."""
        with TempFileManager() as temp_manager:
            return await self._image_handler.process_batch(
                attachments,
                config.worksheet_id,
                row_id,
                column_mapping,
                temp_manager,
                parallel=self._settings.enable_parallel_image_processing,
            )

    def _clear_target_cells(
        self,
        config: ImageToCellConfig,
        row_id: int,
        column_mapping: Dict[int, int],
    ) -> None:
        """Clear target cells before uploading (for replace mode)."""
        # Uploading a new image to a cell replaces the old one automatically
        pass

    @staticmethod
    def filter_prior_for_filled_cells(
        prior_entries: List[Tuple[int, str]],
        row: Dict[str, Any],
        *,
        row_id: int = 0,
    ) -> List[Tuple[int, str]]:
        """
        SH-108: drop prior usage_log entries whose target cell is empty.

        Shared by the backfill estimator (``compute_backfill_estimate``) and
        the runtime synthetic-state builder
        (``ImageProcessor._build_synthetic_states_for_backfill``) so both
        agree on what already counts as done work. Without this, clearing a
        cell after a prior success leaves the estimate at ``images_needed=0``
        while runtime would reprocess the attachment.

        Safety: when the row payload has no ``cells`` at all, returns
        ``prior_entries`` unchanged — better to skip than re-bill on a
        partial Smartsheet response.
        """
        cells_data = row.get("cells") or []
        if not cells_data:
            return list(prior_entries)

        filled_cols = set()
        for cell in cells_data:
            col = cell.get("columnId") or cell.get("column_id")
            img = cell.get("image")
            if isinstance(img, dict) and img.get("id"):
                filled_cols.add(col)

        kept: List[Tuple[int, str]] = []
        for col_id, fname in prior_entries:
            if col_id not in filled_cols:
                log.info(
                    "[SH-108] dropping prior entry row=%s col=%s file=%r — "
                    "cell is currently empty",
                    row_id,
                    col_id,
                    fname,
                )
                continue
            kept.append((col_id, fname))
        return kept

    @staticmethod
    def _prune_stale_state(
        state: RowState,
        attachments: List[Attachment],
        cell_images: Dict[int, Dict[str, Any]],
        columns_with_images: set,
        config: ImageToCellConfig,
        row_id: int,
    ) -> None:
        """
        SH-108 live-path: drop entries from `state.locked_columns` and
        `state.last_ids` whose backing cell is currently empty.

        Mutates `state` in place. Idempotent: re-running on already-clean
        state is a no-op. Persists only if the caller saves `state` later;
        if processing is skipped and `save()` does not run, the next call
        re-derives the same pruning.

        Two kinds of stale entries are pruned:

        - **Locked columns**: indices in `state.locked_columns` whose
          column ID is no longer in `columns_with_images` (the live row's
          set of cells that hold an image). These would otherwise keep
          default mode from routing a new attachment into the cleared
          slot.
        - **Signatures**: entries in `state.last_ids` whose corresponding
          attachment is still on the row but whose filename does not
          appear as `altText` on any cell of the config — meaning the
          cell was cleared. These would otherwise be reported by
          `processed_count` and trip `default.should_process_row` into
          returning False before the new attachment is even considered.

        Signatures for attachments that are no longer on the row are
        kept (defensive — they cause no harm and removing them would
        risk re-billing if the attachment briefly disappears from a
        partial response).
        """
        # Prune locked_columns
        image_cols_list = list(config.image_columns or [])
        stale_locks = [
            col_idx for col_idx in state.locked_columns
            if 0 <= col_idx < len(image_cols_list)
            and image_cols_list[col_idx] not in columns_with_images
        ]
        if stale_locks:
            log.info(
                "[SH-108] row=%s unlocking stale column index(es) %s — cells empty",
                row_id, stale_locks,
            )
            state.locked_columns = [
                i for i in state.locked_columns if i not in stale_locks
            ]

        # Prune last_ids
        if state.last_ids:
            cell_alts = {
                _strip_hash_suffix(img.get("altText"))
                for img in cell_images.values()
                if isinstance(img, dict) and img.get("altText")
            }
            att_by_signature = {a.signature: a for a in attachments}
            stale_sigs = {
                sig for sig in state.last_ids
                if (att := att_by_signature.get(sig)) is not None
                and att.name not in cell_alts
            }
            if stale_sigs:
                log.info(
                    "[SH-108] row=%s dropping %d stale signature(s) — cells cleared",
                    row_id, len(stale_sigs),
                )
                state.last_ids = state.last_ids - stale_sigs
                # Recompute last_ts from remaining on-row signatures. Leaving
                # the old watermark makes default.get_new_attachments treat
                # the just-unmarked attachments as "already seen" even though
                # their cells are empty (SH-183 staging: estimate 66, execute
                # skipped 40 in 76ms).
                remaining_ts = [
                    att_by_signature[sig].created_timestamp
                    for sig in state.last_ids
                    if sig in att_by_signature
                ]
                prev_ts = state.last_ts
                state.last_ts = max(remaining_ts) if remaining_ts else 0.0
                if state.last_ts != prev_ts:
                    log.info(
                        "[SH-108] row=%s reset last_ts %s → %s after clearing "
                        "stale signatures",
                        row_id,
                        prev_ts,
                        state.last_ts,
                    )

    @staticmethod
    def _extract_cell_images(
        row: Dict[str, Any],
        config: ImageToCellConfig,
    ) -> Dict[int, Dict[str, Any]]:
        """
        Return {column_id: cell.image dict} for every cell of this config
        that currently holds an image in the live row payload.

        Smartsheet cell payloads include an `image` dict ({id, altText,
        width, height}) when a cell holds an image — either a manual
        upload or one we placed previously. Cells without an image have
        no `image` key. We keep the whole dict (not just the id) because
        the tier decision compares attachment.name against image.altText,
        and the dict is small.
        """
        config_col_ids = {c for c in (getattr(config, "image_columns", None) or []) if c}
        if not config_col_ids:
            return {}

        out: Dict[int, Dict[str, Any]] = {}
        for cell in row.get("cells", []) or []:
            col_id = cell.get("columnId") or cell.get("column_id")
            if col_id not in config_col_ids:
                continue
            image = cell.get("image")
            if isinstance(image, dict) and image.get("id"):
                out[col_id] = image
        return out

    @staticmethod
    def _apply_existing_image_tiers(
        to_process: List[Attachment],
        column_mapping: Dict[int, int],
        cell_images: Dict[int, Dict[str, Any]],
        mode_name: str,
        is_scan_context: bool,
        row_id: int,
    ) -> List[tuple]:
        """
        SH-105 tiered decision per (attachment, target_col) pair:

        - T1: cell already holds an image whose `altText` matches the
          attachment filename → skip on every mode. Smartsheet stores the
          original filename in altText when an image is placed into a
          cell, so a name match is a strong signal that the attachment is
          the same file the cell already shows.
        - T2: cell holds a different image AND mode is default, OR mode is
          replace running in a scan/backfill context → defensive skip so
          we never overwrite a manual upload during a sweep.
        - T3: cell holds a different image AND mode is name_map (filename
          already matched the column) or replace running from a webhook
          (explicit user intent on a brand new attachment) → process.
        - T4: cell is empty → process.

        Attachments without a target column (default ran out of available
        slots) are dropped silently — they will be considered again on the
        next run if a slot frees up.
        """
        survivors: List[tuple] = []
        for idx, att in enumerate(to_process):
            col_id = column_mapping.get(idx)
            if col_id is None:
                continue

            existing_image = cell_images.get(col_id)
            if not existing_image:
                # T4 — empty target cell, proceed as today.
                survivors.append((att, col_id))
                continue

            existing_alt = existing_image.get("altText")
            if existing_alt and att.name == _strip_hash_suffix(existing_alt):
                # T1 — filename of the attachment equals the cell image's
                # altText (after stripping i2c-3's upload hash suffix),
                # treat as same file and skip.
                log.info(
                    "[SH-105:T1] row=%s col=%s skip — attachment %r already lives in this cell",
                    row_id, col_id, att.name,
                )
                continue

            if mode_name == "default":
                # T2 — default never overwrites an image (manual or otherwise).
                log.info(
                    "[SH-105:T2] row=%s col=%s skip — default mode and cell already has an image",
                    row_id, col_id,
                )
                continue

            if mode_name == "replace":
                if is_scan_context:
                    # T2 — replace running in a scan: defensive skip so a
                    # backfill never silently destroys a manual upload.
                    log.info(
                        "[SH-105:T2] row=%s col=%s skip — replace+scan and cell already has an image",
                        row_id, col_id,
                    )
                    continue
                # T3 — replace from a webhook is an explicit, user-driven
                # event for a brand new attachment; honour replace semantics.
                survivors.append((att, col_id))
                continue

            if mode_name == "name map":
                # T3 — filename matched the column, the user expects
                # name_map to keep that column in sync with the filename.
                survivors.append((att, col_id))
                continue

            # Unknown mode — be defensive and skip rather than guess.
            log.warning(
                "[SH-105] row=%s col=%s skip — unknown mode %r, defaulting to defensive",
                row_id, col_id, mode_name,
            )

        return survivors
