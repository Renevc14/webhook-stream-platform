"""Smoke test that the bolt-test scaffolding loads — fixtures import cleanly,
factories produce valid Pydantic models, mocks behave as documented.

If this file fails, every other bolt test in this directory will fail with
the same root cause; check this one first.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tests.unit.conftest import make_attachment, make_config, make_row


def test_make_attachment_has_signature_and_timestamp():
    att = make_attachment(id=1, name="hello.jpg")
    assert att.id == 1
    assert att.name == "hello.jpg"
    # signature is "{id}:{name}:{created_timestamp}" per src.models.attachment
    assert att.signature.startswith("1:hello.jpg:")
    assert att.created_timestamp > 0


def test_make_attachment_explicit_created_at():
    ts = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    att = make_attachment(id=42, name="x.png", created_at=ts)
    assert att.created_at == ts
    assert att.created_timestamp == ts.timestamp()


def test_make_config_default_mode_three_columns():
    cfg = make_config(config_type="default", n_image_columns=3)
    assert cfg.config_type.value == "default"
    assert len(cfg.image_columns) == 3
    # Column IDs are large 64-bit BIGINTs as in real Smartsheet
    assert all(c > 8_000_000_000_000_000 for c in cfg.image_columns)


def test_make_config_replace_mode_one_column():
    cfg = make_config(config_type="replace", n_image_columns=1, images_to_process=1)
    assert cfg.config_type.value == "replace"
    assert len(cfg.image_columns) == 1


def test_make_config_name_map_mode_uses_column_names():
    cfg = make_config(config_type="name map", n_image_columns=2, column_name_prefix="Front")
    assert cfg.image1_column_name == "Front-1"
    assert cfg.image2_column_name == "Front-2"


def test_make_row_holds_attachments():
    a1 = make_attachment(id=1, name="a.jpg")
    a2 = make_attachment(id=2, name="b.jpg")
    row = make_row(row_id=999, attachments=[a1, a2])
    assert row["id"] == 999
    assert row["attachments"] == [a1, a2]


def test_mock_supabase_returns_configured_rows(mock_supabase):
    mock_supabase.usage_log_rows = [
        {"row_id": 1, "image_column_id": 100, "image_filename": "a.jpg"},
        {"row_id": 1, "image_column_id": 101, "image_filename": "b.jpg"},
    ]
    chain = mock_supabase.table("imagetocell_usage_log")
    result = chain.select("...").eq("organization_id", "x").execute()
    assert len(result.data) == 2
    assert result.data[0]["image_filename"] == "a.jpg"


def test_mock_supabase_raises_when_error_configured(mock_supabase):
    mock_supabase.usage_log_error = RuntimeError("simulated supabase outage")
    chain = mock_supabase.table("imagetocell_usage_log")
    try:
        chain.execute()
    except RuntimeError as e:
        assert "simulated supabase outage" in str(e)
        return
    raise AssertionError("expected RuntimeError")


def test_mock_sheet_service_returns_attachments_from_row(mock_sheet_service):
    a1 = make_attachment(id=1, name="a.jpg")
    a2 = make_attachment(id=2, name="b.jpg")
    row = make_row(row_id=1, attachments=[a1, a2])
    extracted = mock_sheet_service.extract_row_attachments(row, images_only=True)
    assert extracted == [a1, a2]
