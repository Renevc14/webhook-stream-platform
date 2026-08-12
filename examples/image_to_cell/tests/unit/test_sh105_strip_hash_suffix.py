"""
SH-105 hash-suffix stripping tests.

Bug context: i2c-3 renames every attachment during the `/cellimages`
upload by suffixing the basename with `_<uuid.uuid4().hex[:8]>` (see
`utils/image_utils.TempFileManager.create_temp_file`). Smartsheet stores
that renamed filename in `cell.image.altText`. Result: when a sheet was
populated by a previous backfill (templates, cloned sheets, prior runs)
the cell's altText looks like `dog.2_b3c0dc04.jpg` while the source
attachment is still called `dog.2.jpg`, so the literal-match SH-105
anti-dup never fires and the attachment is re-processed + re-billed.

The fix normalises altTexts with `_strip_hash_suffix` before comparing.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import make_attachment, make_cell, make_config


class TestStripHashSuffix:
    def test_strips_8_hex_suffix(self):
        from src.core.row_processor import _strip_hash_suffix

        assert _strip_hash_suffix("dog.2_b3c0dc04.jpg") == "dog.2.jpg"
        assert _strip_hash_suffix("photo_a1b2c3d4.png") == "photo.png"
        assert _strip_hash_suffix("doc_00000000.pdf") == "doc.pdf"

    def test_accepts_uppercase_hex(self):
        from src.core.row_processor import _strip_hash_suffix

        assert _strip_hash_suffix("dog.2_B3C0DC04.jpg") == "dog.2.jpg"

    def test_preserves_filenames_without_suffix(self):
        from src.core.row_processor import _strip_hash_suffix

        # Manual upload via Smartsheet UI — no suffix added
        assert _strip_hash_suffix("manual.jpg") == "manual.jpg"
        # Custom filename a user might have
        assert _strip_hash_suffix("my photo.png") == "my photo.png"

    def test_idempotent(self):
        from src.core.row_processor import _strip_hash_suffix

        stripped_once = _strip_hash_suffix("dog.2_b3c0dc04.jpg")
        stripped_twice = _strip_hash_suffix(stripped_once)
        assert stripped_once == stripped_twice == "dog.2.jpg"

    def test_ignores_short_or_non_hex_suffix(self):
        """Underscore followed by non-8-hex must NOT be stripped — the user
        may have legitimate underscores in their filenames."""
        from src.core.row_processor import _strip_hash_suffix

        assert _strip_hash_suffix("dog_2.jpg") == "dog_2.jpg"  # 1 char
        assert _strip_hash_suffix("photo_red.jpg") == "photo_red.jpg"  # word, not hex
        assert _strip_hash_suffix("file_abc123.jpg") == "file_abc123.jpg"  # 6 chars
        assert _strip_hash_suffix("name_abcdef0123.jpg") == "name_abcdef0123.jpg"  # 10 chars

    def test_handles_none_and_empty(self):
        from src.core.row_processor import _strip_hash_suffix

        assert _strip_hash_suffix(None) is None
        assert _strip_hash_suffix("") == ""

    def test_only_strips_last_segment(self):
        """If the filename has multiple `_<hex>` segments, only the trailing
        one before the extension is stripped (the one i2c-3 actually added)."""
        from src.core.row_processor import _strip_hash_suffix

        # Two suffixes — i2c-3 ran twice on this file (unlikely but defensive)
        assert _strip_hash_suffix("dog.2_aaaaaaaa_bbbbbbbb.jpg") == "dog.2_aaaaaaaa.jpg"


class TestAntiDupWithHashSuffix:
    """End-to-end check that SH-105 anti-dup fires when the cell's altText
    carries i2c-3's hash suffix — the exact scenario the user reported on a
    template-populated sheet."""

    def test_anti_dup_fires_on_template_populated_cell(self):
        from src.core.row_processor import _strip_hash_suffix

        # The cell currently shows the i2c-3-renamed filename
        cell_image_alt = "dog.2_19730d19.jpg"
        # The attachment about to be considered carries the original name
        attachment_name = "dog.2.jpg"

        existing_alts = {_strip_hash_suffix(cell_image_alt)}
        assert attachment_name in existing_alts, (
            "anti-dup must recognise the renamed cell image as the same file "
            "as the source attachment"
        )


class TestTier1WithHashSuffix:
    """End-to-end check that SH-105 T1 (filename match) fires when the
    cell's altText carries the hash suffix."""

    def test_T1_fires_when_cell_altText_has_suffix(self):
        from src.core.row_processor import RowProcessor

        att = make_attachment(id=1, name="dog.33.jpg")
        cell_images = {
            100: {
                "id": "IMG-X",
                "altText": "dog.33_13b1b09b.jpg",  # i2c-3-renamed
                "width": 100, "height": 100,
            },
        }
        survivors = RowProcessor._apply_existing_image_tiers(
            to_process=[att],
            column_mapping={0: 100},
            cell_images=cell_images,
            mode_name="default",
            is_scan_context=True,
            row_id=999,
        )
        assert survivors == [], "T1 must skip when names match after suffix strip"


class TestSH108PruneWithHashSuffix:
    """SH-108 live path uses the same altText comparison — must benefit
    from the strip too. Cell that still legitimately has the file (with
    suffix) should NOT have the corresponding signature dropped."""

    def test_signature_kept_when_cell_altText_has_suffix(self):
        from src.core.row_processor import RowProcessor
        from src.models.row_state import RowState

        cfg = make_config(config_type="default", n_image_columns=2, images_to_process=2)
        col_a, _col_b = cfg.image_columns
        att = make_attachment(id=1, name="dog.2.jpg")

        state = RowState(config_id=cfg.id, row_id=999)
        state.last_ids.add(att.signature)
        state.lock_column(0)

        # Cell still has the i2c-3-renamed image
        cell_images = {
            col_a: {"id": "IMG-A", "altText": "dog.2_b3c0dc04.jpg"},
        }
        columns_with_images = {col_a}

        RowProcessor._prune_stale_state(
            state, [att], cell_images, columns_with_images, cfg, row_id=999
        )

        # Signature must NOT be dropped — the cell still legitimately
        # holds dog.2 (under its renamed altText).
        assert att.signature in state.last_ids, (
            "post-strip cell_alts still contains 'dog.2.jpg' so the signature "
            "should be kept"
        )
        assert state.locked_columns == [0], "lock should be preserved too"
