"""
SH-112 / SH-220 — name_map matcher honours "WHERE filename CONTAINS X".

SH-112: ``namemap_column1..10`` drive CONTAINS matching (case-insensitive
substring of the full filename including extension).

SH-220: blank/null CONTAINS does **not** fall back to fuzzy-matching the
attachment filename against ``image{i}_column_name``. That legacy path
caused silent partial matches (e.g. ``Asset.jpg`` → column Asset while
``Image 2026-07-20….jpg`` on the same row was skipped).
"""

from __future__ import annotations

from tests.unit.conftest import make_attachment, make_config


# ---------------------------------------------------------------------------
# Field plumbing
# ---------------------------------------------------------------------------


class TestNamemapFieldsLoadIntoModel:
    def test_model_accepts_namemap_columns(self):
        from src.models.config import ImageToCellConfig

        cfg = ImageToCellConfig(
            id="t",
            organization_id="org",
            worksheet_id=1,
            configuration_name="t",
            config_type="name map",
            images_to_process=2,
            image1_column_id=100,
            image1_column_name="Primary Column",
            image2_column_id=200,
            image2_column_name="Column2",
            namemap_column1="dog",
            namemap_column2="horse",
        )
        assert cfg.namemap_column1 == "dog"
        assert cfg.namemap_column2 == "horse"
        assert cfg.namemap_column3 is None

    def test_model_namemap_columns_default_to_none(self):
        from src.models.config import ImageToCellConfig

        cfg = ImageToCellConfig(
            id="t", organization_id="org", worksheet_id=1,
            configuration_name="t", config_type="default",
            images_to_process=1, image1_column_id=100, image1_column_name="X",
        )
        assert cfg.namemap_column1 is None
        assert cfg.namemap_column10 is None


# ---------------------------------------------------------------------------
# Matcher behaviour — SH-112 CONTAINS path
# ---------------------------------------------------------------------------


class TestContainsPatternMatching:
    def test_user_scenario_dog_matches_col1(self):
        """attachments dog.1.jpg / horse.1.jpg with CONTAINS dog / horse."""
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            namemap_patterns={1: "dog", 2: "horse"},
        )
        att_dog = make_attachment(id=1, name="dog.1.jpg")
        col_id = NameMapMode()._find_matching_column(att_dog, cfg)
        assert col_id == cfg.image_columns[0]

        att_horse = make_attachment(id=2, name="horse.1.jpg")
        col_id = NameMapMode()._find_matching_column(att_horse, cfg)
        assert col_id == cfg.image_columns[1]

    def test_pattern_match_is_case_insensitive(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map", n_image_columns=1,
            namemap_patterns={1: "DOG"},
        )
        att = make_attachment(id=1, name="dog.1.jpg")
        assert NameMapMode()._find_matching_column(att, cfg) == cfg.image_columns[0]

        cfg = make_config(
            config_type="name map", n_image_columns=1,
            namemap_patterns={1: "dog"},
        )
        att = make_attachment(id=2, name="DOG.1.JPG")
        assert NameMapMode()._find_matching_column(att, cfg) == cfg.image_columns[0]

    def test_first_matching_pattern_wins(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map", n_image_columns=2,
            namemap_patterns={1: "dog", 2: "1"},
        )
        att = make_attachment(id=1, name="dog.1.jpg")
        assert NameMapMode()._find_matching_column(att, cfg) == cfg.image_columns[0]

    def test_no_match_returns_none(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map", n_image_columns=2,
            namemap_patterns={1: "dog", 2: "horse"},
        )
        att = make_attachment(id=1, name="cat.5.jpg")
        assert NameMapMode()._find_matching_column(att, cfg) is None

    def test_pattern_substring_match_works_inside_filename(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map", n_image_columns=1,
            namemap_patterns={1: "report"},
        )
        for filename in ("Q4-report.pdf", "monthly_report_2026.jpg", "thereport.png"):
            att = make_attachment(id=1, name=filename)
            assert NameMapMode()._find_matching_column(att, cfg) == cfg.image_columns[0]


# ---------------------------------------------------------------------------
# SH-220 — blank CONTAINS must not fuzzy-match column names
# ---------------------------------------------------------------------------


class TestBlankContainsNoLegacyFallback:
    def test_beth_asset_blank_contains_matches_nothing(self):
        """Beth's row: column Asset, blank CONTAINS — neither Asset.jpg nor
        a timestamped camera filename should route via column-name fuzziness."""
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map",
            n_image_columns=1,
            column_name_prefix="Asset",
            namemap_patterns=None,
        )
        # make_config names columns "Asset-1"; override to plain "Asset"
        cfg.image1_column_name = "Asset"

        mode = NameMapMode()
        assert mode._find_matching_column(
            make_attachment(id=1, name="Asset.jpg"), cfg
        ) is None
        assert mode._find_matching_column(
            make_attachment(id=2, name="Image 2026-07-20 09-25-57.jpg"), cfg
        ) is None

    def test_explicit_contains_asset_matches_only_asset_filename(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map",
            n_image_columns=1,
            column_name_prefix="Asset",
            namemap_patterns={1: "Asset"},
        )
        cfg.image1_column_name = "Asset"
        mode = NameMapMode()
        assert mode._find_matching_column(
            make_attachment(id=1, name="Asset.jpg"), cfg
        ) == cfg.image_columns[0]
        assert mode._find_matching_column(
            make_attachment(id=2, name="Image 2026-07-20 09-25-57.jpg"), cfg
        ) is None

    def test_blank_patterns_never_match_photo_column_name(self):
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map",
            n_image_columns=2,
            column_name_prefix="Photo",
            namemap_patterns=None,
        )
        att = make_attachment(id=1, name="Photo-1.jpg")
        assert NameMapMode()._find_matching_column(att, cfg) is None

    def test_pattern_slot_does_not_fall_back_to_column_name(self):
        """Slot has CONTAINS 'horse' — filename matching column name is ignored."""
        from src.core.modes.name_map import NameMapMode

        cfg = make_config(
            config_type="name map",
            n_image_columns=1,
            column_name_prefix="dog",
            namemap_patterns={1: "horse"},
        )
        att = make_attachment(id=1, name="dog-1.jpg")
        assert NameMapMode()._find_matching_column(att, cfg) is None
