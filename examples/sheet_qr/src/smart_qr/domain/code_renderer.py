"""Barcode / QR PNG renderer (pure aside from Pillow)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Literal, Optional

import qrcode
from barcode import Code128, EAN13
from barcode.writer import ImageWriter
from PIL import Image

from smart_qr.config.constants import (
    DEFAULT_QR_BACK,
    DEFAULT_QR_BORDER,
    DEFAULT_QR_BOX,
    DEFAULT_QR_ECC,
    DEFAULT_QR_FILL,
    DEFAULT_QR_LOGO_SCALE,
    MAX_QR_LOGO_SCALE,
    MIN_QR_LOGO_SCALE,
)

CodeType = Literal["qr", "code128", "ean"]

_HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass(frozen=True)
class RenderOptions:
    code_type: CodeType = "qr"
    qr_ecc: str = DEFAULT_QR_ECC
    qr_box_size: int = DEFAULT_QR_BOX
    qr_border: int = DEFAULT_QR_BORDER
    logo_png: Optional[bytes] = None
    fill_color: str = DEFAULT_QR_FILL
    back_color: str = DEFAULT_QR_BACK
    logo_scale: float = DEFAULT_QR_LOGO_SCALE


class RenderError(ValueError):
    pass


_ECC = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}


def normalize_hex_color(value: str | None, default: str) -> str:
    raw = (value or "").strip() or default
    if not _HEX_COLOR.match(raw):
        raise RenderError(f"invalid color: {value!r} (use #RGB or #RRGGBB)")
    if len(raw) == 4:
        return "#" + "".join(ch * 2 for ch in raw[1:])
    return raw.upper()


def clamp_logo_scale(value: float | None) -> float:
    try:
        scale = float(value if value is not None else DEFAULT_QR_LOGO_SCALE)
    except (TypeError, ValueError):
        scale = DEFAULT_QR_LOGO_SCALE
    return max(MIN_QR_LOGO_SCALE, min(MAX_QR_LOGO_SCALE, scale))


def clamp_border(value: int | None) -> int:
    try:
        border = int(value if value is not None else DEFAULT_QR_BORDER)
    except (TypeError, ValueError):
        border = DEFAULT_QR_BORDER
    return max(1, min(4, border))


def clamp_box_size(value: int | None) -> int:
    try:
        box = int(value if value is not None else DEFAULT_QR_BOX)
    except (TypeError, ValueError):
        box = DEFAULT_QR_BOX
    # Allow S/M/L presets and nearby values
    return max(2, min(8, box))


def render_png(payload: str, options: RenderOptions | None = None) -> bytes:
    opts = options or RenderOptions()
    if not payload:
        raise RenderError("empty payload")
    if opts.code_type == "qr":
        return _render_qr(payload, opts)
    if opts.code_type == "code128":
        return _render_code128(payload)
    if opts.code_type == "ean":
        return _render_ean(payload)
    raise RenderError(f"unsupported code_type: {opts.code_type}")


def _render_qr(payload: str, opts: RenderOptions) -> bytes:
    fill = normalize_hex_color(opts.fill_color, DEFAULT_QR_FILL)
    back = normalize_hex_color(opts.back_color, DEFAULT_QR_BACK)
    box = clamp_box_size(opts.qr_box_size)
    border = clamp_border(opts.qr_border)
    # Product always uses H (logo-ready); ignore lower ECC from legacy configs
    qr = qrcode.QRCode(
        version=None,
        error_correction=_ECC["H"],
        box_size=box,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill, back_color=back).convert("RGBA")
    if opts.logo_png:
        img = _paste_logo(img, opts.logo_png, clamp_logo_scale(opts.logo_scale), back)
    return _to_png(img)


def _paste_logo(
    qr_img: Image.Image,
    logo_bytes: bytes,
    logo_scale: float,
    back_color: str,
) -> Image.Image:
    """Center a logo on the QR with a pad matching the QR background."""
    try:
        logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"invalid logo image: {exc}") from exc

    qr_w, qr_h = qr_img.size
    logo_max = max(int(min(qr_w, qr_h) * logo_scale), 16)
    logo.thumbnail((logo_max, logo_max), Image.Resampling.LANCZOS)
    lw, lh = logo.size
    pad = max(int(logo_max * 0.12), 4)
    box_w, box_h = lw + pad * 2, lh + pad * 2
    try:
        pad_rgb = tuple(int(back_color[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    except Exception:  # noqa: BLE001
        pad_rgb = (255, 255, 255, 255)
    badge = Image.new("RGBA", (box_w, box_h), pad_rgb)
    badge.paste(logo, (pad, pad), logo)
    pos = ((qr_w - box_w) // 2, (qr_h - box_h) // 2)
    out = qr_img.copy()
    out.paste(badge, pos, badge)
    return out


def _render_code128(payload: str) -> bytes:
    buf = io.BytesIO()
    Code128(payload, writer=ImageWriter()).write(buf, options={"write_text": False})
    buf.seek(0)
    return buf.read()


def _render_ean(payload: str) -> bytes:
    digits = "".join(c for c in payload if c.isdigit())
    if len(digits) not in {12, 13}:
        raise RenderError("EAN-13 requires 12 or 13 digits")
    if len(digits) == 13:
        digits = digits[:12]
    buf = io.BytesIO()
    EAN13(digits, writer=ImageWriter()).write(buf, options={"write_text": False})
    buf.seek(0)
    return buf.read()


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
