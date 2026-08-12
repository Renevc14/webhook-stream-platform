"""
Regression tests for usage_log unique-violation handling.

Migration 20260803190000 drops the SH-98 unique index so reprocess can
insert additional billable success rows. The wrapper still classifies
residual 23505 errors as ``ALREADY_LOGGED`` (pre-migration staging, race).
Callers that completed a real image write must keep the Redis charge —
they must not refund solely because of this sentinel.

These tests exercise:

1. Happy path: RPC succeeds, returns the new UUID.
2. Unique violation surfaced via .code attribute (postgrest-py APIError).
3. Unique violation surfaced via message substring (fallback for
   library-version drift).
4. Genuine transient failure (network, timeout, etc.) — must still emit
   ERROR-level log so operators see it.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from src.services.usage_log import ALREADY_LOGGED, log_image_conversion


class _FakePostgrestError(Exception):
    """Mimics postgrest-py APIError with a ``.code`` attribute matching the
    Postgres SQLSTATE for the rejected statement."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _kwargs():
    """Common kwargs for log_image_conversion. Values are deterministic so
    tests are easy to read."""
    return dict(
        organization_id="00000000-0000-0000-0000-000000000001",
        user_id="11111111-1111-1111-1111-111111111111",
        config_id="22222222-2222-2222-2222-222222222222",
        config_name="my-config",
        config_type="default",
        worksheet_id=5651291798720388,
        worksheet_name="my-sheet",
        row_id=4640729026985860,
        image_column_id=753507497185156,
        image_column_name="image",
        image_filename="photo.jpg",
        success=True,
    )


@patch("src.services.usage_log.get_supabase_service")
def test_returns_uuid_on_successful_insert(mock_get_svc):
    """Baseline: the RPC returns a UUID and we propagate it as a str."""
    expected_id = "33333333-3333-3333-3333-333333333333"
    db = MagicMock()
    db.client.rpc.return_value.execute.return_value.data = expected_id
    mock_get_svc.return_value = db

    result = log_image_conversion(**_kwargs())

    assert result == expected_id


@patch("src.services.usage_log.get_supabase_service")
def test_unique_violation_via_code_attribute_returns_already_logged_without_error(
    mock_get_svc, caplog
):
    """When the RPC raises an exception whose ``.code`` is the Postgres
    unique_violation SQLSTATE, the wrapper must return ALREADY_LOGGED —
    no ERROR-level log line, INFO line documents what happened. Callers
    that wrote the image keep the Redis hold (do not refund on sentinel).
    """
    db = MagicMock()
    db.client.rpc.return_value.execute.side_effect = _FakePostgrestError(
        "23505",
        'duplicate key value violates unique constraint '
        '"imagetocell_usage_log_unique_per_processing_idx"',
    )
    mock_get_svc.return_value = db

    with caplog.at_level(logging.INFO, logger="src.services.usage_log"):
        result = log_image_conversion(**_kwargs())

    assert result == ALREADY_LOGGED
    # No noisy ERROR for the expected dedup case
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not error_records, (
        f"unexpected ERROR-level logs for unique_violation case: "
        f"{[r.getMessage() for r in error_records]}"
    )
    # We do leave an INFO breadcrumb so operators can see dedup happening
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "[USAGE_LOG]" in r.getMessage() and "Duplicate prevented" in r.getMessage()
        for r in info_records
    ), f"expected INFO breadcrumb missing, got: {[r.getMessage() for r in info_records]}"


@patch("src.services.usage_log.get_supabase_service")
def test_unique_violation_via_substring_fallback_returns_already_logged(
    mock_get_svc, caplog
):
    """If the exception type does not expose ``.code`` (e.g. supabase-py
    version drift wraps differently), the substring match on the message
    must still classify the error as a unique_violation."""
    db = MagicMock()
    db.client.rpc.return_value.execute.side_effect = RuntimeError(
        "PostgREST returned error 23505: duplicate key value violates unique constraint"
    )
    mock_get_svc.return_value = db

    with caplog.at_level(logging.INFO, logger="src.services.usage_log"):
        result = log_image_conversion(**_kwargs())

    assert result == ALREADY_LOGGED
    assert not any(r.levelname == "ERROR" for r in caplog.records)


@patch("src.services.usage_log.get_supabase_service")
def test_unique_violation_via_index_name_substring_returns_already_logged(
    mock_get_svc, caplog
):
    """A third-party wrapper may strip the SQLSTATE but keep the index
    name. That alone is enough to classify the error."""
    db = MagicMock()
    db.client.rpc.return_value.execute.side_effect = RuntimeError(
        'API error: constraint "imagetocell_usage_log_unique_per_processing_idx" failed'
    )
    mock_get_svc.return_value = db

    with caplog.at_level(logging.INFO, logger="src.services.usage_log"):
        result = log_image_conversion(**_kwargs())

    assert result == ALREADY_LOGGED
    assert not any(r.levelname == "ERROR" for r in caplog.records)


@patch("src.services.usage_log.get_supabase_service")
def test_non_unique_failure_still_logs_error(mock_get_svc, caplog):
    """A real transient failure (network, timeout, server disconnect) must
    NOT be swallowed. The wrapper continues to surface it as ERROR so
    operators see it in alerts."""
    db = MagicMock()
    db.client.rpc.return_value.execute.side_effect = RuntimeError(
        "Server disconnected without sending a response"
    )
    mock_get_svc.return_value = db

    with caplog.at_level(logging.ERROR, logger="src.services.usage_log"):
        result = log_image_conversion(**_kwargs())

    assert result is None
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "expected ERROR log for genuine transient failure"
    assert "Failed to log image conversion" in error_records[0].getMessage()
