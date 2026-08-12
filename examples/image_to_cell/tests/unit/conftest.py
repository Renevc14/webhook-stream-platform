"""
Shared pytest fixtures for I2C bolt-calculation unit tests.

These tests focus on the synthetic state build + budget preflight that decide
whether a backfill processes (and charges for) an attachment that was already
processed previously. The reported bug — "in large default-mode backfills, all
attachments get reprocessed instead of only the new ones" — lives in this code
path, so we exercise it heavily here.

All Supabase calls are mocked; no external services are required.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Env setup — required so importing src.config.settings does not fail
# ---------------------------------------------------------------------------

_BASE_ENV: Dict[str, str] = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "APP_CRYPTO_KEY": "0123456789abcdef0123456789abcdef",
    "WEBHOOK_LIFECYCLE_ENABLED": "false",
    "DLQ_PROCESSOR_ENABLED": "false",
    "ENABLE_RATE_LIMITING": "false",
    "I2C_BOLT_GATE_ENABLED": "true",
}


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the env vars required for imports, scoped per test."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _fake_redis_client_for_smartsheet_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SH-100: ``get_smartsheet_client`` requires Redis to be wired by the
    app lifespan. Unit tests don't run lifespan, so we install a MagicMock
    on the shared accessor for every test. Tests that need different
    behaviour can override this with their own ``set_redis_client(...)``.
    The Smartsheet calls themselves are mocked elsewhere — the mock here
    only needs to be non-None so the factory does not raise the
    ``SH-93: SmartsheetClient requires Redis`` error.
    """
    from src.services.redis_client import set_redis_client

    set_redis_client(MagicMock(name="FakeRedisClient"))
    yield
    set_redis_client(None)


# ---------------------------------------------------------------------------
# Factories — produce models with sensible defaults
# ---------------------------------------------------------------------------


def make_attachment(
    *,
    id: int,
    name: str,
    created_at: Optional[datetime] = None,
    mime_type: str = "image/jpeg",
):
    """Build a Smartsheet Attachment model for tests.

    Defaults to a JPEG with a deterministic created_at so signatures
    are reproducible across runs.
    """
    from src.models.attachment import Attachment

    if created_at is None:
        # Deterministic default — keeps signatures stable in test output and
        # ensures every attachment has a distinct timestamp regardless of `id`.
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=id)

    return Attachment(
        id=id,
        name=name,
        mime_type=mime_type,
        created_at=created_at,
    )


def make_config(
    *,
    config_type: str = "default",
    n_image_columns: int = 3,
    images_to_process: int = 3,
    config_id: str = "cfg-test",
    organization_id: str = "11111111-1111-1111-1111-111111111111",
    worksheet_id: int = 7000000000000001,
    column_id_base: int = 8100000000000000,
    column_name_prefix: str = "Image",
    namemap_patterns: Optional[Dict[int, str]] = None,
):
    """Build an ImageToCellConfig with N image columns.

    `column_id_base + i` is used so column IDs are large 64-bit BIGINTs
    (matches Smartsheet ID size). `column_name_prefix-{i}` is used as the
    column name (relevant for name_map mode tests).

    Pass `namemap_patterns={1: "dog", 2: "horse"}` to populate the
    `namemap_column{i}` fields used by SH-112's name_map matcher.
    """
    from src.models.config import ImageToCellConfig

    if n_image_columns < 1 or n_image_columns > 10:
        raise ValueError("n_image_columns must be 1..10")

    fields: Dict[str, Any] = {
        "id": config_id,
        "organization_id": organization_id,
        "worksheet_id": worksheet_id,
        "configuration_name": "test config",
        "config_type": config_type,
        "images_to_process": images_to_process,
    }
    for i in range(1, n_image_columns + 1):
        fields[f"image{i}_column_id"] = column_id_base + i
        fields[f"image{i}_column_name"] = f"{column_name_prefix}-{i}"
    if namemap_patterns:
        for i, pattern in namemap_patterns.items():
            fields[f"namemap_column{i}"] = pattern

    return ImageToCellConfig(**fields)


def make_row(
    *,
    row_id: int,
    attachments: List[Any],
    cells: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a Smartsheet row dict containing the given attachments.

    Pass `cells` to simulate the cells payload Smartsheet returns alongside
    the row — needed by SH-105/SH-108 tests that inspect cell.image to
    decide whether to skip or trust the prior usage_log entry.
    """
    row: Dict[str, Any] = {"id": row_id, "attachments": attachments}
    if cells is not None:
        row["cells"] = cells
    return row


def make_cell(
    *,
    column_id: int,
    image_alt: Optional[str] = None,
    image_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Smartsheet cell dict.

    If `image_alt` is provided, the cell carries an `image` dict mimicking
    a cell with an embedded image (manual upload or prior backfill output).
    A default `image_id` is synthesised from the alt text when omitted.
    """
    cell: Dict[str, Any] = {"columnId": column_id}
    if image_alt:
        cell["image"] = {
            "id": image_id or f"img-{image_alt}",
            "altText": image_alt,
            "width": 100,
            "height": 100,
        }
    return cell


# ---------------------------------------------------------------------------
# Mocks — Supabase + SheetService
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supabase():
    """A SupabaseService mock with configurable `imagetocell_usage_log` data.

    Configure by setting `mock_supabase.usage_log_rows` to a list of dicts
    matching the columns selected by the production query:
        {"row_id": int, "image_column_id": int, "image_filename": str}

    The mock raises `mock_supabase.usage_log_error` (if set) instead of
    returning data — useful for fail-open path tests.

    Supports pagination semantics:
      - `chain.in_("row_id", [...])` filters rows by row_id (so chunked
        queries return only the entries for the chunk).
      - `chain.range(start, end)` slices the filtered result (PostgREST
        range is inclusive on both ends, returning up to end-start+1 rows).
    Each `execute()` resets the per-call filter state, mirroring how
    PostgREST chains behave in production.

    Inspect `svc.execute_call_count` to verify how many DB roundtrips
    happened (useful for chunk/pagination tests).
    """
    svc = MagicMock(name="SupabaseService")
    svc.usage_log_rows = []  # type: ignore[attr-defined]
    svc.usage_log_error: Optional[Exception] = None  # type: ignore[attr-defined]
    svc.execute_call_count = 0  # type: ignore[attr-defined]

    chain = MagicMock()

    # Per-call state; reset inside execute() so each query starts clean.
    state: Dict[str, Any] = {"row_ids_filter": None, "range": None}

    def fake_in(field: str, values: Any) -> Any:
        if field == "row_id":
            state["row_ids_filter"] = list(values)
        return chain

    def fake_range(start: int, end: int) -> Any:
        state["range"] = (start, end)
        return chain

    def execute() -> Any:
        svc.execute_call_count += 1  # type: ignore[attr-defined]
        try:
            if svc.usage_log_error is not None:  # type: ignore[attr-defined]
                raise svc.usage_log_error  # type: ignore[attr-defined]
            rows = list(svc.usage_log_rows)  # type: ignore[attr-defined]

            if state["row_ids_filter"] is not None:
                allowed = set(state["row_ids_filter"])
                rows = [r for r in rows if r["row_id"] in allowed]

            if state["range"] is not None:
                start, end = state["range"]
                rows = rows[start : end + 1]

            result = MagicMock()
            result.data = rows
            return result
        finally:
            # Reset per-call filters — the next chain may not specify them
            state["row_ids_filter"] = None
            state["range"] = None

    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.in_.side_effect = fake_in
    chain.range.side_effect = fake_range
    chain.execute.side_effect = execute

    svc.table = MagicMock(return_value=chain)
    svc.client = MagicMock()
    return svc


@pytest.fixture
def mock_sheet_service():
    """A SheetService mock whose `extract_row_attachments(row, images_only=True)`
    returns whatever is in `row["attachments"]`.

    Real SheetService inspects `row["attachments"]` and filters by mime type;
    this mock skips that and returns the attachments verbatim, since the row
    factory already provides image-only attachments.
    """
    svc = MagicMock(name="SheetService")

    def extract_row_attachments(row: Dict[str, Any], images_only: bool = True):
        return list(row.get("attachments", []))

    svc.extract_row_attachments.side_effect = extract_row_attachments
    return svc


@pytest.fixture
def processor(mock_supabase, monkeypatch: pytest.MonkeyPatch):
    """ImageProcessor wired with mock Supabase for both call paths:
        - processor._db (used by ConfigRepository / StateRepository)
        - i2c_bolt_gate.get_supabase_service (used by the shared
          _fetch_prior_processed retry helper that
          _build_synthetic_states_for_backfill now delegates to)

    Also tightens the retry backoff to ~zero so retry-exhaustion tests
    don't pay the production wall-clock cost.
    """
    from src.core.processor import ImageProcessor
    from src.services import i2c_bolt_gate

    monkeypatch.setattr(i2c_bolt_gate, "get_supabase_service", lambda: mock_supabase)
    monkeypatch.setattr(i2c_bolt_gate, "_PRIOR_LOG_RETRY_BASE_DELAY_S", 0.001)
    return ImageProcessor(supabase=mock_supabase)
