import pytest

from smart_qr.domain.code_renderer import RenderError, RenderOptions, render_png
from smart_qr.domain.hashutil import payload_hash
from smart_qr.domain.payload_builder import (
    PayloadBuildError,
    build_advanced_template,
    build_form_prefill_payload,
    build_identify_payload,
    build_open_row_payload,
)
from smart_qr.domain.write_policy import should_write


def test_identify_payload():
    r = build_identify_payload(["Asset ID"], {"Asset ID": "A-1"})
    assert r.text == "A-1"
    assert r.content_mode == "identify"


def test_identify_empty_raises():
    with pytest.raises(PayloadBuildError, match="no non-empty"):
        build_identify_payload(["Asset ID"], {"Asset ID": "  "})


def test_identify_all_empty_columns_raises():
    """Empty watched cells must not encode as '||' and bill a write."""
    with pytest.raises(PayloadBuildError, match="no non-empty"):
        build_identify_payload(
            ["Nombre", "Apellido", "Telefono"],
            {"Nombre": "", "Apellido": " ", "Telefono": ""},
        )


def test_form_prefill_url():
    r = build_form_prefill_payload(
        "https://app.smartsheet.com/b/form/abc",
        [("Asset ID", "Asset ID")],
        {"Asset ID": "42"},
    )
    assert "Asset%20ID=42" in r.text or "Asset+ID=42" in r.text
    assert r.text.startswith("https://app.smartsheet.com/b/form/abc")


def test_form_prefill_all_empty_raises():
    with pytest.raises(PayloadBuildError, match="no non-empty"):
        build_form_prefill_payload(
            "https://app.smartsheet.com/b/form/abc",
            [("Nombre", "Nombre"), ("Apellido", "Apellido")],
            {"Nombre": "", "Apellido": "  "},
        )


def test_open_row_permalink():
    r = build_open_row_payload(
        "https://app.smartsheet.com/sheets/123?rowId=456"
    )
    assert r.content_mode == "open_row"
    assert "rowId=456" in r.text


def test_open_row_empty_raises():
    with pytest.raises(PayloadBuildError):
        build_open_row_payload("  ")


def test_advanced_template_replaces_placeholders():
    r = build_advanced_template(
        "https://apps.example.com/asset/{Asset ID}?lat={Lat}",
        {"Asset ID": "A-42", "Lat": "40.1"},
    )
    assert r.content_mode == "advanced"
    assert r.text == "https://apps.example.com/asset/A-42?lat=40.1"


def test_advanced_template_empty_raises():
    with pytest.raises(PayloadBuildError, match="empty"):
        build_advanced_template("", {"Asset ID": "A-1"})


def test_advanced_template_unresolved_placeholder_raises():
    with pytest.raises(PayloadBuildError, match="unresolved"):
        build_advanced_template(
            "https://apps.example.com/{Asset ID}/{Missing}",
            {"Asset ID": "A-1"},
        )


def test_hash_stable():
    assert payload_hash("x", "qr") == payload_hash("x", "qr")
    assert payload_hash("x", "qr") != payload_hash("x", "code128")


def test_write_policy():
    assert should_write("replace", True) is True
    assert should_write("fill_if_empty", True) is False
    assert should_write("fill_if_empty", False) is True


def test_render_qr_png():
    png = render_png("hello-smartqr", RenderOptions(code_type="qr"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_qr_with_logo():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (32, 32), color=(0, 128, 255)).save(buf, format="PNG")
    png = render_png(
        "https://app.smartsheet.com/sheets/1?rowId=2",
        RenderOptions(code_type="qr", logo_png=buf.getvalue()),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_qr_colored():
    png = render_png(
        "brand-demo",
        RenderOptions(
            code_type="qr",
            fill_color="#0EA5E9",
            back_color="#0F172A",
            qr_box_size=3,
            qr_border=1,
            logo_scale=0.2,
        ),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_code128():
    png = render_png("ASSET42", RenderOptions(code_type="code128"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_ean():
    png = render_png("5901234123457", RenderOptions(code_type="ean"))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_ean_bad_length():
    with pytest.raises(RenderError):
        render_png("123", RenderOptions(code_type="ean"))
