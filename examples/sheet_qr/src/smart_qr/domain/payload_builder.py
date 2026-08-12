"""Payload construction for SmartQR (pure, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence
from urllib.parse import quote, urlencode, urlparse, urlunparse, parse_qsl

from smart_qr.config.constants import MAX_PAYLOAD_CHARS


class PayloadBuildError(ValueError):
    """Invalid/empty payload — row should be skipped (not billed)."""


class PayloadSourceError(ValueError):
    """Source cell/column is incompatible (e.g. cell image) — row is an error."""


@dataclass(frozen=True)
class PayloadResult:
    text: str
    content_mode: str


def build_identify_payload(
    column_titles: Sequence[str],
    cell_values: Mapping[str, str],
    *,
    separator: str = "|",
) -> PayloadResult:
    if not column_titles:
        raise PayloadBuildError("identify mode requires at least one content column")
    parts: list[str] = []
    any_value = False
    for title in column_titles:
        val = (cell_values.get(title) or "").strip()
        if val:
            any_value = True
        parts.append(val)
    # All-empty must not become "||" (truthy) — that billed empty rows as barcodes
    if not any_value:
        raise PayloadBuildError("identify payload has no non-empty content values")
    text = separator.join(parts).strip()
    if not text:
        raise PayloadBuildError("identify payload is empty after render")
    return _bounded(PayloadResult(text=text, content_mode="identify"))


def build_form_prefill_payload(
    form_base_url: str,
    mappings: Sequence[tuple[str, str]],
    cell_values: Mapping[str, str],
) -> PayloadResult:
    """mappings: list of (form_label, column_title)."""
    if not form_base_url or not form_base_url.strip():
        raise PayloadBuildError("form_base_url is required")
    parsed = urlparse(form_base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PayloadBuildError("form_base_url must be an absolute http(s) URL")
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    any_value = False
    for form_label, column_title in mappings:
        if not form_label:
            raise PayloadBuildError("form label cannot be empty")
        val = (cell_values.get(column_title) or "").strip()
        if val:
            any_value = True
        existing[form_label] = val
    if not any_value:
        raise PayloadBuildError("form_prefill payload has no non-empty content values")
    query = urlencode(existing, quote_via=quote)
    text = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment)
    )
    return _bounded(PayloadResult(text=text, content_mode="form_prefill"))


def build_open_row_payload(permalink_url: str) -> PayloadResult:
    """QR payload is the Smartsheet row permalink (opens that row for shared users)."""
    url = (permalink_url or "").strip()
    if not url:
        raise PayloadBuildError("open_row requires a row permalink URL")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PayloadBuildError("open_row permalink must be an absolute http(s) URL")
    return _bounded(PayloadResult(text=url, content_mode="open_row"))


def build_advanced_template(
    template: str,
    cell_values: Mapping[str, str],
) -> PayloadResult:
    if not template:
        raise PayloadBuildError("advanced template is empty")
    out = template
    # Replace longer column names first to avoid partial overlaps
    for title in sorted(cell_values.keys(), key=len, reverse=True):
        out = out.replace("{" + title + "}", cell_values.get(title) or "")
    if "{" in out and "}" in out:
        raise PayloadBuildError(f"unresolved placeholders in template: {out[:120]}")
    text = out.strip()
    if not text:
        raise PayloadBuildError("advanced template rendered empty")
    return _bounded(PayloadResult(text=text, content_mode="advanced"))


def _bounded(result: PayloadResult) -> PayloadResult:
    if len(result.text) > MAX_PAYLOAD_CHARS:
        raise PayloadBuildError(
            f"payload exceeds {MAX_PAYLOAD_CHARS} characters ({len(result.text)})"
        )
    return result
