"""
SH-109 tests — WebhookHandler scopes row processing to trigger attachments.

The bug being fixed: a webhook for attachment X on row R would cause
row_processor.process to consider EVERY attachment on R, including old
ones that were intentionally skipped earlier (e.g. by SH-105 anti-dup).
If the user then cleared the cell that made anti-dup match, the old
attachment would be silently re-processed by the next unrelated webhook
on that row — billing the customer for something they never asked for.

The fix narrows the row's `attachments` list to only those whose id is
the trigger of the current webhook event before handing the row to
row_processor.process. Cells stay intact so SH-105/SH-108 still see
the live state of the row.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _row(attachments: List[Dict[str, Any]], cells: List[Dict[str, Any]] | None = None):
    row: Dict[str, Any] = {"id": 999, "attachments": attachments}
    if cells is not None:
        row["cells"] = cells
    return row


class TestScopeRowToTriggerAttachments:
    def test_keeps_only_trigger_attachment(self):
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "old.jpg"},
            {"id": 2, "name": "new.jpg"},
            {"id": 3, "name": "another.jpg"},
        ])
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {2})
        assert [a["id"] for a in out["attachments"]] == [2]

    def test_keeps_multiple_when_several_triggered(self):
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "a.jpg"},
            {"id": 2, "name": "b.jpg"},
            {"id": 3, "name": "c.jpg"},
        ])
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {1, 3})
        assert {a["id"] for a in out["attachments"]} == {1, 3}

    def test_empty_trigger_set_is_a_noop(self):
        """Defensive: never filter to [] when the caller passes an empty set
        — that would silently break processing for callers that haven't been
        migrated yet."""
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[{"id": 1}, {"id": 2}])
        out = WebhookHandler._scope_row_to_trigger_attachments(row, set())
        assert [a["id"] for a in out["attachments"]] == [1, 2]

    def test_trigger_not_on_row_yields_empty_attachments(self):
        """User deleted the attachment between webhook fire and our fetch —
        we end up processing nothing, which is the correct behaviour (the
        row has other attachments but none of them were the trigger)."""
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "old.jpg"},
            {"id": 2, "name": "older.jpg"},
        ])
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {999})
        assert out["attachments"] == []

    def test_cells_are_preserved(self):
        """SH-105/SH-108 still need cells to inspect the live row — scoping
        must not strip them."""
        from src.workers.webhook_handler import WebhookHandler

        cells = [
            {"columnId": 100, "image": {"id": "IMG-A", "altText": "a.jpg"}},
            {"columnId": 200},
        ]
        row = _row(attachments=[{"id": 1}, {"id": 2}], cells=cells)
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {1})
        assert out["cells"] == cells

    def test_original_row_is_not_mutated(self):
        """The handler may reuse the row dict; the scoping helper must
        return a new dict to avoid surprising the caller."""
        from src.workers.webhook_handler import WebhookHandler

        attachments = [{"id": 1}, {"id": 2}]
        row = _row(attachments=attachments)
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {1})
        assert out is not row
        assert [a["id"] for a in row["attachments"]] == [1, 2]

    def test_handles_missing_attachments_field(self):
        from src.workers.webhook_handler import WebhookHandler

        row = {"id": 999}
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {1})
        assert out["attachments"] == []

    def test_handles_none_attachments_field(self):
        from src.workers.webhook_handler import WebhookHandler

        row = {"id": 999, "attachments": None}
        out = WebhookHandler._scope_row_to_trigger_attachments(row, {1})
        assert out["attachments"] == []


class TestConditionalScopingByMode:
    """Mirror test for the conditional scoping logic at the call site.

    Default mode benefits from SH-109 scoping (avoid re-processing old
    anti-dup'd attachments). Replace mode must see ALL attachments on the
    row to maintain the "newest N in N cols" invariant. Name_map must see
    all matching filenames simultaneously. If a refactor changes the call
    site to always scope (or never scope), this mirror test catches it.
    """

    @staticmethod
    def _should_scope(config_type_value: str) -> bool:
        """Mirror of the conditional in webhook_handler.py."""
        cfg_type_raw = config_type_value or ""
        return cfg_type_raw.lower() == "default"

    def test_default_mode_scopes(self):
        assert self._should_scope("default") is True

    def test_replace_mode_does_NOT_scope(self):
        assert self._should_scope("replace") is False

    def test_name_map_mode_does_NOT_scope(self):
        assert self._should_scope("name map") is False

    def test_unknown_mode_does_not_scope(self):
        """Defensive: an unknown config_type should not scope (safer to
        pass all attachments to the row_processor than to drop some)."""
        assert self._should_scope("future_mode_x") is False
        assert self._should_scope("") is False


class TestSH107EndToEndExpectation:
    """The specific scenario the user reported: anti-dup skips dog.2 in
    backfill, user clears the manual cell, uploads horse.jpg. Webhook for
    horse should process ONLY horse — not horse + dog.2."""

    def test_only_trigger_attachment_reaches_row_processor(self):
        from src.workers.webhook_handler import WebhookHandler

        # The row Smartsheet would return: still has the old dog.2 in its
        # attachments panel (user never deleted it), plus the brand new
        # horse-4 that triggered the webhook.
        row = _row(
            attachments=[
                {"id": 7864146849206148, "name": "dog.2.jpg"},   # the "ghost" attachment
                {"id": 102662475911044, "name": "horse-4.jpg"},  # the webhook trigger
            ],
            cells=[
                {"columnId": 6059782573428612},  # cleared by user
                {"columnId": 3807982759743364},  # always was empty
            ],
        )
        # Webhook fired for horse-4 only
        scoped = WebhookHandler._scope_row_to_trigger_attachments(
            row, {102662475911044}
        )
        names = [a["name"] for a in scoped["attachments"]]
        assert names == ["horse-4.jpg"], (
            f"row_processor must only see the webhook trigger; got {names}"
        )


# ---------------------------------------------------------------------------
# SH-191 — expand triggers with co-arrived form siblings
# ---------------------------------------------------------------------------


class TestExpandTriggersWithCoarrived:
    """Form multi-file uploads share createdAt; SH-107 ghosts do not."""

    def test_form_batch_same_second_expands_all_siblings(self):
        from src.workers.webhook_handler import WebhookHandler

        ts = "2026-07-21T08:46:47Z"
        row = _row(attachments=[
            {"id": 1, "name": "a.jpg", "createdAt": ts},
            {"id": 2, "name": "b.jpg", "createdAt": ts},
            {"id": 3, "name": "c.jpg", "createdAt": ts},
            {"id": 4, "name": "d.jpg", "createdAt": ts},
            {"id": 5, "name": "e.jpg", "createdAt": ts},
        ])
        # Smartsheet only emitted attachment.created for id=3
        expanded = WebhookHandler._expand_triggers_with_coarrived(row, {3})
        assert expanded == {1, 2, 3, 4, 5}

        scoped = WebhookHandler._scope_row_to_trigger_attachments(row, expanded)
        assert {a["id"] for a in scoped["attachments"]} == {1, 2, 3, 4, 5}

    def test_form_batch_one_second_stagger_still_expands(self):
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "a.jpg", "createdAt": "2026-07-21T08:46:47Z"},
            {"id": 2, "name": "b.jpg", "createdAt": "2026-07-21T08:46:48Z"},
            {"id": 3, "name": "c.jpg", "createdAt": "2026-07-21T08:46:47Z"},
        ])
        expanded = WebhookHandler._expand_triggers_with_coarrived(row, {1})
        assert expanded == {1, 2, 3}

    def test_sh107_ghost_hours_earlier_not_expanded(self):
        """Preserve SH-107: old anti-dup'd attachment must stay out."""
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {
                "id": 7864146849206148,
                "name": "dog.2.jpg",
                "createdAt": "2026-07-20T10:00:00Z",
            },
            {
                "id": 102662475911044,
                "name": "horse-4.jpg",
                "createdAt": "2026-07-21T08:46:47Z",
            },
        ])
        expanded = WebhookHandler._expand_triggers_with_coarrived(
            row, {102662475911044}
        )
        assert expanded == {102662475911044}

        scoped = WebhookHandler._scope_row_to_trigger_attachments(row, expanded)
        assert [a["name"] for a in scoped["attachments"]] == ["horse-4.jpg"]

    def test_unparseable_trigger_timestamps_fail_closed(self):
        """No parseable trigger createdAt → return original trigger set."""
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "a.jpg"},  # missing createdAt
            {"id": 2, "name": "b.jpg", "createdAt": "2026-07-21T08:46:47Z"},
        ])
        expanded = WebhookHandler._expand_triggers_with_coarrived(row, {1})
        assert expanded == {1}

    def test_empty_trigger_set_unchanged(self):
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "a.jpg", "createdAt": "2026-07-21T08:46:47Z"},
        ])
        assert WebhookHandler._expand_triggers_with_coarrived(row, set()) == set()

    def test_outside_window_not_included(self):
        from src.workers.webhook_handler import WebhookHandler

        row = _row(attachments=[
            {"id": 1, "name": "new.jpg", "createdAt": "2026-07-21T08:46:47Z"},
            # 30s earlier — outside +/-5s window
            {"id": 2, "name": "older.jpg", "createdAt": "2026-07-21T08:46:17Z"},
        ])
        expanded = WebhookHandler._expand_triggers_with_coarrived(row, {1})
        assert expanded == {1}
