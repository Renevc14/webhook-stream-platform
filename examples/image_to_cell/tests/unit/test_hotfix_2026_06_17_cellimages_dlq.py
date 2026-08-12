"""Hotfix 2026-06-17 (Beth incident, sheet 695569967173508).

Two bugs combined to leave 106 rows stuck for Beth:

  1. ``smarthub_core.platforms.smartsheet.client.RETRYABLE_PATTERNS`` did not
     include "500", "4004", or any "shouldRetry" pattern (pre-hotfix). After
     the Beth hotfix those patterns live on the async ``SmartsheetClient``.
  2. The row-batch path in ``webhook_handler.py`` stored failed events with
     ``"attachment_ids"`` (plural list) while ``dlq_processor.py`` read
     ``"attachment_id"`` (singular). Every row-batch DLQ entry was
     abandoned with "No attachment_id in event data", so the 5-minute
     auto-retry could never recover the 106 events.

This file pins the post-fix contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# RC1 — retry pattern coverage for 4004 + shouldRetry
# ---------------------------------------------------------------------------


class TestRetryablePatternsForBethIncident:
    @pytest.mark.parametrize(
        "raised_message",
        [
            # Real Smartsheet error shapes seen in CloudWatch during the
            # 2026-06-16 burst. Different SDK versions / wrappers format
            # the same payload slightly differently — we accept all of them.
            '{"statusCode":500,"errorCode":4004,"message":"Unexpected error","shouldRetry":true,"refId":"iyaf7a"}',
            'Error: {"errorCode": 4004, "shouldRetry": true, "message": "Unexpected error."}',
            # The 4000 variant we already had — still covered.
            '{"errorCode":4000,"message":"Smartsheet flake"}',
            # 500 substring alone catches some SDK shapes.
            "HTTP 500 Internal Server Error",
            # Defense in depth: any future code Smartsheet emits with the
            # "shouldRetry": true flag will retry without a code change.
            'Smartsheet response: {"errorCode": 9999, "shouldRetry": true}',
        ],
    )
    @pytest.mark.asyncio
    async def test_message_is_marked_retryable(self, raised_message: str):
        """After SH-102 the retry list lives on smarthub_core SmartsheetClient."""
        from smarthub_core.platforms.smartsheet.client import (
            SmartsheetClient,
            is_retryable_smartsheet_error,
        )

        assert is_retryable_smartsheet_error(Exception(raised_message)) is True

        attempts = {"count": 0}

        def operation(_client):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Exception(raised_message)
            return "ok-on-retry"

        client = SmartsheetClient.__new__(SmartsheetClient)
        with patch.object(
            client, "_wait_for_rate_limit_slot", new=AsyncMock()
        ), patch.object(
            client, "get_client", new=AsyncMock(return_value=MagicMock())
        ), patch(
            "smarthub_core.platforms.smartsheet.client.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await client.call_with_retry_async(
                operation, description="test", max_retries=3, base_delay=0.0
            )

        assert result == "ok-on-retry"
        assert attempts["count"] == 2

    @pytest.mark.parametrize(
        "raised_message",
        [
            "401 Unauthorized",
            "404 Not Found: sheet does not exist",
            '{"errorCode": 1004, "shouldRetry": false}',  # auth error, do NOT retry
        ],
    )
    @pytest.mark.asyncio
    async def test_non_retryable_messages_still_raise(self, raised_message: str):
        """We must not accidentally start retrying 4xx errors when we added 4004."""
        from smarthub_core.platforms.smartsheet.client import (
            SmartsheetClient,
            is_retryable_smartsheet_error,
        )

        assert is_retryable_smartsheet_error(Exception(raised_message)) is False

        def operation(_client):
            raise Exception(raised_message)

        client = SmartsheetClient.__new__(SmartsheetClient)
        with patch.object(
            client, "_wait_for_rate_limit_slot", new=AsyncMock()
        ), patch.object(
            client, "get_client", new=AsyncMock(return_value=MagicMock())
        ), patch(
            "smarthub_core.platforms.smartsheet.client.asyncio.sleep",
            new=AsyncMock(),
        ):
            with pytest.raises(Exception):
                await client.call_with_retry_async(
                    operation, description="test", max_retries=3, base_delay=0.0
                )


# ---------------------------------------------------------------------------
# RC2 — DLQ accepts both attachment_id (singular) and attachment_ids (plural)
# ---------------------------------------------------------------------------


class TestDlqAcceptsBothAttachmentIdShapes:
    """The row-batch path in webhook_handler.py stores `attachment_ids`
    (plural list). The single-event path stores `attachment_id` (singular).
    Before the hotfix the DLQ reader only handled the singular shape and
    abandoned every batch entry with "No attachment_id in event data".
    """

    def _build_processor(self):
        from src.workers.dlq_processor import DLQProcessor

        proc = DLQProcessor.__new__(DLQProcessor)
        # The lookup-by-attachment path needs the processor + repo only when
        # the row_id isn't already in the event_data, which we avoid in this
        # set of tests.
        proc._processor = MagicMock(name="ImageProcessor")
        proc._dlq_repo = MagicMock(name="DLQRepo")
        return proc

    @pytest.mark.asyncio
    async def test_plural_attachment_ids_does_not_abort(self):
        """Before the fix this event_data shape (the row-batch path's writer)
        was rejected with 'No attachment_id in event data'."""
        from src.workers.dlq_processor import DLQProcessor, FailedEvent

        proc = self._build_processor()

        config = MagicMock(organization_id="org-test", worksheet_id=999)
        event = MagicMock(spec=FailedEvent)
        event.event_data = {
            "row_id": 12345,                          # we have the row, no lookup needed
            "attachment_ids": [111, 222, 333],        # PLURAL list
            "failed_count": 3,
        }
        event.id = "evt-test"

        with patch("src.workers.dlq_processor.get_smartsheet_client"), \
             patch("src.workers.dlq_processor.SheetService") as MockSheet, \
             patch("src.workers.dlq_processor.AttachmentService"):
            MockSheet.return_value.get_row = AsyncMock(
                return_value={"id": 12345, "cells": []}
            )
            mock_result = MagicMock()
            mock_result.images_processed = 1
            mock_result.images_failed = 0
            mock_result.image_results = []
            proc._processor.process_row = AsyncMock(return_value=mock_result)

            ok = await proc._retry_attachment_event(event, config)

        # Pre-fix this returned False with "No attachment_id" — now it must
        # proceed and return True (row processed successfully).
        assert ok is True

    @pytest.mark.asyncio
    async def test_legacy_singular_attachment_id_still_works(self):
        """Don't regress the original single-event path while fixing batch."""
        from src.workers.dlq_processor import DLQProcessor, FailedEvent

        proc = self._build_processor()

        config = MagicMock(organization_id="org-test", worksheet_id=999)
        event = MagicMock(spec=FailedEvent)
        event.event_data = {
            "row_id": 12345,
            "attachment_id": 7777,   # SINGULAR — legacy single-event shape
        }
        event.id = "evt-test"

        with patch("src.workers.dlq_processor.get_smartsheet_client"), \
             patch("src.workers.dlq_processor.SheetService") as MockSheet, \
             patch("src.workers.dlq_processor.AttachmentService"):
            MockSheet.return_value.get_row = AsyncMock(
                return_value={"id": 12345, "cells": []}
            )
            mock_result = MagicMock()
            mock_result.images_processed = 1
            mock_result.images_failed = 0
            mock_result.image_results = []
            proc._processor.process_row = AsyncMock(return_value=mock_result)

            ok = await proc._retry_attachment_event(event, config)

        assert ok is True

    @pytest.mark.asyncio
    async def test_aborts_when_truly_no_identifiers(self):
        """If event_data has no row_id AND no attachment_id(s), abort. The
        warning should now mention BOTH possible field names so an operator
        can debug downstream writers."""
        from src.workers.dlq_processor import DLQProcessor, FailedEvent

        proc = self._build_processor()

        config = MagicMock(organization_id="org-test", worksheet_id=999)
        event = MagicMock(spec=FailedEvent)
        event.event_data = {"unrelated_field": "x"}
        event.id = "evt-test"

        ok = await proc._retry_attachment_event(event, config)
        assert ok is False


# ---------------------------------------------------------------------------
# Side issue — `includeAll` is deprecated fleet-wide; every i2c-3 caller must
# pass `page_size=10000` instead.
# ---------------------------------------------------------------------------


class TestNoIncludeAllInSheetService:
    """Regression guards for the `includeAll` deprecation (errorCode 1018).

    `SheetService` makes two calls that used to pass `include_all=True`:
    `attachments.list_all_attachments` (SH-120 backfill path) and
    `sheets.get_columns` (column discovery). Both must now pass
    `page_size=10000` and must NOT pass `include_all`. A reintroduction would
    re-trigger the 1018 spam in CloudWatch.
    """

    def _make_client(self):
        client = MagicMock()
        captured = {}

        async def fake_call_with_retry_async(operation, **kwargs):
            sdk = MagicMock()
            result = MagicMock()
            result.to_dict.return_value = {"data": []}
            operation(sdk)
            return result

        client.call_with_retry_async = fake_call_with_retry_async
        client._captured = captured
        return client

    @pytest.mark.asyncio
    async def test_list_attachments_uses_page_size_not_include_all(self):
        from src.services.smartsheet.sheets import SheetService

        client = MagicMock()
        sdk_holder = {}

        async def fake_call(operation, **kwargs):
            sdk = MagicMock()
            result = MagicMock()
            result.to_dict.return_value = {"data": []}
            sdk.Attachments.list_all_attachments.return_value = result
            operation(sdk)
            sdk_holder["call"] = sdk.Attachments.list_all_attachments.call_args
            return result

        client.call_with_retry_async = fake_call
        await SheetService(client)._list_attachments_by_row(sheet_id=123)

        args, kwargs = sdk_holder["call"]
        assert 123 in args or kwargs.get("sheet_id") == 123
        assert "include_all" not in kwargs
        assert kwargs.get("page_size") == 10000

    @pytest.mark.asyncio
    async def test_get_columns_uses_page_size_not_include_all(self):
        from src.services.smartsheet.sheets import SheetService

        client = MagicMock()
        sdk_holder = {}

        async def fake_call(operation, **kwargs):
            sdk = MagicMock()
            result = MagicMock()
            result.to_dict.return_value = {"data": []}
            sdk.Sheets.get_columns.return_value = result
            operation(sdk)
            sdk_holder["call"] = sdk.Sheets.get_columns.call_args
            return result

        client.call_with_retry_async = fake_call
        await SheetService(client).get_columns(sheet_id=456)

        args, kwargs = sdk_holder["call"]
        assert 456 in args or kwargs.get("sheet_id") == 456
        assert "include_all" not in kwargs
        assert kwargs.get("page_size") == 10000


class TestNoIncludeAllInDiscoveryRoutes:
    """The folder/workspace detail endpoints used by the config UI's sheet
    picker previously passed `includeAll=true` as a query parameter. They
    must now pass only `pageSize=10000`.
    """

    @pytest.mark.asyncio
    async def test_walk_folder_does_not_pass_include_all(self):
        from src.api.routes.smartsheet import walk_folder

        client = MagicMock()

        async def fake_get(url, params=None):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json = MagicMock(return_value={
                "name": "Test Folder", "sheets": [], "folders": []
            })
            return response

        client.get = fake_get
        # Capture call args via a separate spy
        calls = []
        async def spy_get(url, params=None):
            calls.append({"url": url, "params": params})
            return await fake_get(url, params=params)
        client.get = spy_get

        output: list = []
        await walk_folder(client, folder_id=42, path="", output=output)

        # Find the /folders/42 call
        folder_call = next(c for c in calls if "/folders/42" in c["url"])
        params = folder_call["params"] or {}
        assert "includeAll" not in params, (
            "walk_folder must not pass includeAll — Smartsheet deprecated it "
            "(errorCode 1018)."
        )
        assert params.get("pageSize") == 10000, (
            "walk_folder must pass pageSize=10000 to preserve the original "
            "'return everything' intent."
        )
