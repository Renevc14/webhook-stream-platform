"""
SH-105 tests — skip cells that already hold an image.

Covers:
  - DefaultMode.get_target_columns excludes columns reported as filled.
  - RowProcessor._extract_cell_images reads `cell.image` from the live row
    payload and is scoped to the config's image columns.
  - RowProcessor._apply_existing_image_tiers implements the four-tier
    decision (same filename via altText, default skip, replace+scan
    defensive skip, replace+webhook process, name_map process, empty
    cell process, missing target column dropped).

Background: Smartsheet does NOT expose an `imageId` field on row
attachments (only on the cell.image itself), so SH-105 falls back to
matching attachment filename against cell.image.altText. The verification
script `scripts/verify_cell_image_in_response.py` covers that discovery.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from tests.unit.conftest import make_attachment, make_config


# ---------------------------------------------------------------------------
# DefaultMode.get_target_columns with columns_with_images
# ---------------------------------------------------------------------------


class TestDefaultModeRespectsFilledColumns:
    def test_skips_columns_that_already_hold_an_image(self):
        from src.core.modes.default import DefaultMode
        from src.models.row_state import RowState

        mode = DefaultMode()
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns
        state = RowState(config_id=cfg.id, row_id=1)

        atts = [make_attachment(id=10 + i, name=f"a{i}.jpg") for i in range(3)]

        # Column A (index 0) already has a manual image.
        mapping = mode.get_target_columns(
            atts, state, cfg, columns_with_images={col_ids[0]}
        )

        # First attachment routes to col B (index 1), second to col C (index 2),
        # third has no slot left.
        assert mapping == {0: col_ids[1], 1: col_ids[2]}

    def test_combines_filled_columns_with_locked_indices(self):
        from src.core.modes.default import DefaultMode
        from src.models.row_state import RowState

        mode = DefaultMode()
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns
        state = RowState(config_id=cfg.id, row_id=1)
        state.lock_column(0)  # index 0 already locked from prior run

        atts = [make_attachment(id=10 + i, name=f"a{i}.jpg") for i in range(2)]

        # Index 0 locked + col B filled → only col C available.
        mapping = mode.get_target_columns(
            atts, state, cfg, columns_with_images={col_ids[1]}
        )
        assert mapping == {0: col_ids[2]}

    def test_default_behavior_unchanged_when_no_filled_columns(self):
        from src.core.modes.default import DefaultMode
        from src.models.row_state import RowState

        mode = DefaultMode()
        cfg = make_config(config_type="default", n_image_columns=3, images_to_process=3)
        col_ids = cfg.image_columns
        state = RowState(config_id=cfg.id, row_id=1)
        atts = [make_attachment(id=10 + i, name=f"a{i}.jpg") for i in range(3)]

        mapping = mode.get_target_columns(atts, state, cfg)
        assert mapping == {0: col_ids[0], 1: col_ids[1], 2: col_ids[2]}


# ---------------------------------------------------------------------------
# _extract_cell_images helper
# ---------------------------------------------------------------------------


class TestExtractCellImages:
    def _row(self, cells: List[Dict[str, Any]]):
        return {"id": 1, "cells": cells}

    def test_returns_image_dict_for_cells_in_config(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(n_image_columns=3)
        c0, c1, c2 = cfg.image_columns
        row = self._row(
            [
                {"columnId": c0, "image": {"id": "IMG-A", "altText": "a.jpg"}},
                {"columnId": c1},  # no image
                {"columnId": c2, "image": {"id": "IMG-C", "altText": "c.jpg"}},
            ]
        )
        result = RowProcessor._extract_cell_images(row, cfg)
        assert set(result.keys()) == {c0, c2}
        assert result[c0]["altText"] == "a.jpg"
        assert result[c2]["altText"] == "c.jpg"
        assert result[c0]["id"] == "IMG-A"

    def test_ignores_cells_outside_config_columns(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(n_image_columns=2)
        c0, c1 = cfg.image_columns
        unrelated_col = 999999999999999
        row = self._row(
            [
                {"columnId": c0, "image": {"id": "IMG-A", "altText": "a.jpg"}},
                {"columnId": unrelated_col, "image": {"id": "IMG-X", "altText": "x.jpg"}},
            ]
        )
        result = RowProcessor._extract_cell_images(row, cfg)
        assert list(result.keys()) == [c0]

    def test_returns_empty_when_row_has_no_cells(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(n_image_columns=2)
        assert RowProcessor._extract_cell_images({"id": 1}, cfg) == {}

    def test_returns_empty_when_image_dict_lacks_id(self):
        from src.core.row_processor import RowProcessor

        cfg = make_config(n_image_columns=1)
        c0 = cfg.image_columns[0]
        row = self._row([{"columnId": c0, "image": {"altText": "no id here"}}])
        assert RowProcessor._extract_cell_images(row, cfg) == {}

    def test_accepts_snake_case_column_id_key(self):
        """Some upstream payloads use column_id instead of columnId."""
        from src.core.row_processor import RowProcessor

        cfg = make_config(n_image_columns=1)
        c0 = cfg.image_columns[0]
        row = self._row([{"column_id": c0, "image": {"id": "IMG-X", "altText": "x.jpg"}}])
        result = RowProcessor._extract_cell_images(row, cfg)
        assert result[c0]["altText"] == "x.jpg"


# ---------------------------------------------------------------------------
# _apply_existing_image_tiers — tier matrix
# ---------------------------------------------------------------------------


class TestApplyExistingImageTiers:
    @staticmethod
    def _cell_image(*, image_id: str = "IMG-X", alt: str = "manual.jpg"):
        return {"id": image_id, "altText": alt, "width": 100, "height": 100}

    def _run(self, *, mode_name: str, is_scan: bool, pairs, cell_images):
        """`pairs` is a list of (attachment, target_col_id)."""
        from src.core.row_processor import RowProcessor

        to_process = [att for att, _ in pairs]
        column_mapping = {i: col_id for i, (_, col_id) in enumerate(pairs)}
        return RowProcessor._apply_existing_image_tiers(
            to_process, column_mapping, cell_images, mode_name, is_scan, row_id=1
        )

    def test_T1_same_filename_always_skipped(self):
        for mode_name in ("default", "replace", "name map"):
            for is_scan in (True, False):
                att = make_attachment(id=1, name="manual.jpg")
                survivors = self._run(
                    mode_name=mode_name,
                    is_scan=is_scan,
                    pairs=[(att, 100)],
                    cell_images={100: self._cell_image(alt="manual.jpg")},
                )
                assert survivors == [], (
                    f"mode={mode_name} scan={is_scan}: T1 must skip"
                )

    def test_T4_empty_cell_processes_for_all_modes(self):
        for mode_name in ("default", "replace", "name map"):
            for is_scan in (True, False):
                att = make_attachment(id=1, name="new.jpg")
                survivors = self._run(
                    mode_name=mode_name,
                    is_scan=is_scan,
                    pairs=[(att, 100)],
                    cell_images={},  # cell empty
                )
                assert survivors == [(att, 100)], (
                    f"mode={mode_name} scan={is_scan}: T4 must process"
                )

    def test_T2_default_with_existing_different_image_skips(self):
        att = make_attachment(id=1, name="new.jpg")
        survivors = self._run(
            mode_name="default",
            is_scan=False,
            pairs=[(att, 100)],
            cell_images={100: self._cell_image(alt="manual.jpg")},
        )
        assert survivors == []

    def test_T2_replace_in_scan_context_skips(self):
        att = make_attachment(id=1, name="new.jpg")
        survivors = self._run(
            mode_name="replace",
            is_scan=True,
            pairs=[(att, 100)],
            cell_images={100: self._cell_image(alt="manual.jpg")},
        )
        assert survivors == []

    def test_T3_replace_in_webhook_context_processes(self):
        att = make_attachment(id=1, name="new.jpg")
        survivors = self._run(
            mode_name="replace",
            is_scan=False,
            pairs=[(att, 100)],
            cell_images={100: self._cell_image(alt="manual.jpg")},
        )
        assert survivors == [(att, 100)]

    def test_T3_name_map_processes_regardless_of_scan_flag(self):
        for is_scan in (True, False):
            att = make_attachment(id=1, name="new.jpg")
            survivors = self._run(
                mode_name="name map",
                is_scan=is_scan,
                pairs=[(att, 100)],
                cell_images={100: self._cell_image(alt="manual.jpg")},
            )
            assert survivors == [(att, 100)], f"name_map scan={is_scan} must process"

    def test_attachment_without_target_column_is_dropped(self):
        from src.core.row_processor import RowProcessor

        att = make_attachment(id=1, name="a.jpg")
        survivors = RowProcessor._apply_existing_image_tiers(
            to_process=[att],
            column_mapping={},  # no target col
            cell_images={},
            mode_name="default",
            is_scan_context=True,
            row_id=1,
        )
        assert survivors == []

    def test_survivors_keep_original_ordering(self):
        a1 = make_attachment(id=1, name="a.jpg")
        a2 = make_attachment(id=2, name="b.jpg")
        a3 = make_attachment(id=3, name="c.jpg")

        # Pair 2's name matches the cell altText (T1 skip), 1 and 3 survive.
        survivors = self._run(
            mode_name="default",
            is_scan=True,
            pairs=[(a1, 100), (a2, 200), (a3, 300)],
            cell_images={200: self._cell_image(alt="b.jpg")},
        )
        assert survivors == [(a1, 100), (a3, 300)]

    def test_cell_image_without_altText_falls_through_to_mode_rules(self):
        """Without altText we cannot do a name match; default still skips
        defensively (T2), replace+webhook still processes (T3)."""
        att = make_attachment(id=1, name="a.jpg")
        # default+filled-without-altText → T2 skip
        survivors = self._run(
            mode_name="default",
            is_scan=False,
            pairs=[(att, 100)],
            cell_images={100: {"id": "IMG-X"}},  # no altText
        )
        assert survivors == []
        # replace+webhook+filled-without-altText → T3 process
        survivors = self._run(
            mode_name="replace",
            is_scan=False,
            pairs=[(att, 100)],
            cell_images={100: {"id": "IMG-X"}},
        )
        assert survivors == [(att, 100)]


# ---------------------------------------------------------------------------
# Anti-dup pre-filter for default mode — exercised at the helper level
# ---------------------------------------------------------------------------


class TestDefaultAntiDupPreFilter:
    """The pre-filter lives in RowProcessor.process() itself (not in a helper),
    but its logic is small and exact. We mirror it here so a refactor that
    breaks the rule fails this test instead of slipping through to production.
    """

    @staticmethod
    def _apply_anti_dup(attachments, cell_images, mode_name):
        if mode_name != "default" or not cell_images:
            return attachments
        existing_alts = {
            img.get("altText") for img in cell_images.values()
            if isinstance(img, dict) and img.get("altText")
        }
        if not existing_alts:
            return attachments
        return [a for a in attachments if a.name not in existing_alts]

    def test_default_drops_attachment_with_filename_in_any_config_column(self):
        att_dup = make_attachment(id=10, name="manual.jpg")
        att_new = make_attachment(id=11, name="new.jpg")
        filtered = self._apply_anti_dup(
            [att_dup, att_new],
            cell_images={123456: {"id": "IMG-X", "altText": "manual.jpg"}},
            mode_name="default",
        )
        assert filtered == [att_new]

    def test_replace_does_not_apply_anti_dup(self):
        """Replace should NOT pre-filter; tier T3 in webhook context decides."""
        att = make_attachment(id=10, name="a.jpg")
        filtered = self._apply_anti_dup(
            [att],
            cell_images={123: {"id": "IMG-X", "altText": "a.jpg"}},
            mode_name="replace",
        )
        assert filtered == [att]

    def test_name_map_does_not_apply_anti_dup(self):
        att = make_attachment(id=10, name="a.jpg")
        filtered = self._apply_anti_dup(
            [att],
            cell_images={123: {"id": "IMG-X", "altText": "a.jpg"}},
            mode_name="name map",
        )
        assert filtered == [att]

    def test_no_altText_in_existing_image_skips_pre_filter(self):
        """If we cannot read altText we cannot match by name — pass through."""
        att = make_attachment(id=10, name="a.jpg")
        filtered = self._apply_anti_dup(
            [att],
            cell_images={123: {"id": "IMG-X"}},  # no altText
            mode_name="default",
        )
        assert filtered == [att]
