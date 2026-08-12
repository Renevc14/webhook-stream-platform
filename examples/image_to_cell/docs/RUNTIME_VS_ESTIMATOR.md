# Runtime vs Estimator — Parity Reference

> For: i2c-3 (image-to-cell-3).
> Last updated: 2026-06-25.
> Why this doc exists: SH-144 surfaced a divergence between the preview estimator and the runtime billing path. Fix landed in commit becf79d. This doc captures the parity contract so future changes don't silently break it.

## Why parity matters

One paragraph: the customer sees a preview cost in the Create New Config modal (SH-122 → SH-144 endpoint). They then run the backfill. If preview differs from bill, customer trust erodes and support tickets spike. Parity is the contract: the number in the modal MUST equal the bill, modulo sheet changes between the two calls.

## The shared algorithmic identity

Both paths execute the SAME sequence per row:

1. Extract attachments: sheet_service.extract_row_attachments(row, images_only=True).
2. Snapshot cell.image: RowProcessor._extract_cell_images(row, config) returns {column_id: cell.image dict} for cells in this config that hold an image.
3. Build/load state: in runtime, state comes from state_repo (persisted RowState); in estimator, state is built synthetically from _fetch_prior_processed (a query against imagetocell_usage_log filtered by org + worksheet + target_column_ids + row_ids, NOT by config_id — so drafts and existing configs see the same history for overlapping columns).
4. Filter to eligible attachments: mode.filter_attachments(atts, state, config).
5. Map attachments to columns: mode.get_target_columns(eligible, state, config, columns_with_images).
6. Apply tier check: RowProcessor._apply_existing_image_tiers(eligible, column_mapping, cell_images, mode_name, is_scan_context, row_id). Returns survivors as a list of (attachment, col_id) tuples.
7. (Runtime only) Upload survivors to Smartsheet, write usage_log entries, save state.
8. (Estimator only) images_needed += len(survivors).

## Where they differ BY CONSTRUCTION

### Side effects

The runtime writes (Smartsheet uploads via /cellimages, usage_log entries in imagetocell_usage_log, state in imagetocell_row_state). The estimator is pure read. SH-144 has a dedicated regression test (test_sh144_backfill_estimate_draft_config.py::TestNoWriteInvariant::test_draft_path_does_not_write_to_supabase) that mocks every Supabase write verb (insert / update / upsert / delete / execute_with_retry) and asserts none are called when the draft path runs.

### Scan context

The estimator always passes is_scan_context=True to _apply_existing_image_tiers because it is previewing a backfill (a scan, not a live webhook event). The runtime uses is_scan_context based on the trigger: True for backfill / polling scans, False for live webhook events. This affects T2/T3 routing for replace mode:
- Replace + scan + occupied cell → T2 (skip, defensive).
- Replace + webhook + occupied cell → T3 (process, explicit user intent on fresh attachment).

The estimator only models backfill, so it consistently uses the T2 path for replace + occupied. This is correct because the preview is shown before the backfill, and backfill is the scan context.

### Sheet state snapshot timing

The estimator snapshots Smartsheet at preview time. The runtime snapshots at backfill time. If the sheet changes in between (someone adds an attachment, pastes an image, deletes a row), results differ. This is a feature, not a bug. The frontend can mitigate by refreshing the preview right before Save.

## The 4 tiers (T1, T2, T3, T4) — reference table

| Tier | Trigger | Default mode | Replace + scan | Replace + webhook | Name_map | T4 |
|---|---|---|---|---|---|---|
| T1 | Cell altText equals attachment filename | Skip | Skip | Skip | Skip | n/a |
| T2 | Cell has different image | Skip | Skip | Process | (n/a, T3 below) | n/a |
| T3 | Cell has different image, mode-specific | (T2) | (T2) | Process | Process | n/a |
| T4 | Cell empty | Process | Process | Process | Process | Process |

For source: src/core/row_processor.py:524-609 (_apply_existing_image_tiers).

## SH-144's contribution to the estimator

Pre-SH-144 the estimator only ran steps 1-4 then 8 (count len(eligible) directly). It did not extract cell images, did not run get_target_columns, did not apply the tier check. So sheets where files appeared both as attachments AND already pasted into the target cell got over-counted by exactly the manually-pasted set.

Post-SH-144 (commit becf79d) the estimator runs the full sequence: steps 1-2 to snapshot cells, steps 3-4 to filter, steps 5-6 to map and tier-check. The number it produces matches the number the runtime would bill, modulo sheet state changes.

## Edge cases worth knowing

### Name_map T3 always fires in scan context too

ADR-023 captures the decision to keep this. When a name_map pattern matches an attachment, the runtime processes it even if the target column holds a manually-uploaded different image. Counted by both runtime and estimator. Surfaces in modal copy for SH-122.

### Draft estimate uses sentinel config_id "draft-preview"

This sentinel does NOT affect the prior_log lookup. _fetch_prior_processed filters by org + worksheet + column_ids + row_ids, not by config_id. So drafts inherit the visible processing history of any persisted config that processed the same columns on the same worksheet. Correct behaviour — a new config built on a worksheet where another config already processed dogs in col 1 should see those dogs as "already done" and skip them.

### Draft estimate cannot populate image{N}_column_name

The draft payload only includes column IDs from image_columns. The legacy name_map fallback of fuzzy-matching attachment filename against the column display name doesn't work for drafts (image{N}_column_name is None). Workaround: frontend must require at least one explicit namemap_patterns entry when config_type === "name map". Future option: add image_column_names: Optional[List[str]] to DraftConfigPayload.

### Multiple attachments routing to the same column slot (name_map)

If 3 attachments in a row all match a single name_map pattern, all 3 route to slot 1. The runtime processes all 3 sequentially (overwriting in cell), bills all 3. The estimator counts all 3. Confirmed during SH-144 validation. This is the same behaviour as the runtime; both sides agree.

## How to test parity

### Unit test

backend/image-to-cell-3/tests/unit/test_sh144_backfill_estimate_draft_config.py::TestCellContentSkipMatchesRuntime. 6 tests cover every tier outcome:
- test_default_mode_skips_attachment_for_occupied_cell (T2 default)
- test_default_mode_tier_t1_skip_for_same_filename (T1)
- test_replace_mode_scan_skips_occupied_cell (T2 replace+scan)
- test_empty_cells_process_normally (T4)
- test_row_without_cells_payload_falls_back_safely (no-cells fall-back)
- test_all_cells_occupied_returns_zero_for_default_mode (full T2 cover)

### Real-sheet test

Call POST /configs/backfill/estimate against a worksheet with a known mix of:
- Empty target cells + fresh attachments (T4)
- Target cell with manual upload, same filename as an attachment (T1)
- Target cell with manual upload, different filename (T2 for default/replace+scan, T3 for name_map)
- Multiple attachments matching a single name_map pattern (multi-route count)

Record bolts_needed in the response. Hit POST /configs to create the actual config, then POST /configs/backfill to run the backfill. Compare bills.

## Links

- SH-105 — origin of the tier semantic.
- SH-112 — name_map CONTAINS pattern.
- SH-121 — pre-backfill estimate endpoint (parent of SH-144).
- SH-144 — draft_config extension + cell-content skip fix.
- ADR-022 — SH-144 architectural decision (filename: `022-backfill-estimate-draft-config.md`).
- ADR-023 — name_map T3 decision (filename: `023-name-map-t3-semantic.md`).
- src/services/i2c_bolt_gate.py:268-484 — compute_backfill_estimate.
- src/core/row_processor.py:97+ — RowProcessor.process (runtime).
- src/core/row_processor.py:494+ — _extract_cell_images (shared static method).
- src/core/row_processor.py:524+ — _apply_existing_image_tiers (shared static method).
- src/core/modes/{default,replace,name_map}.py — mode-specific filter_attachments and get_target_columns.
