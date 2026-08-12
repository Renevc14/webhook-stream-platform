"""
Regression tests for the production incident 2026-05-15.

Smartsheet sometimes batches multiple object types in a single webhook
delivery (attachment + discussion + proof + reminder + ...). Before this
fix, the WebhookEvent model used strict Pydantic Enum validation on
``object_type`` and ``event_type``, so a single unknown value rejected the
entire payload. The route also passed the raw events list to
WebhookPayload instead of the pre-filtered ``attachment_events`` list,
amplifying the failure.

These tests cover the two defensive layers:

1. The model accepts any string for ``object_type`` / ``event_type`` and
   classifies via the ``is_*`` properties without crashing.
2. WebhookPayload can be built from a mixed-objectType list directly, or
   from the pre-filtered attachment_events list (defence in depth).

If any of these tests starts failing, do NOT tighten the model — instead
re-read the incident notes in ``backend/image-to-cell-3/CHANGELOG.md`` and
``docs/tools/image-to-cell/GAPS_AND_ISSUES.md`` issue #20.
"""

import pytest

from src.models.webhook import (
    WebhookEvent,
    WebhookEventType,
    WebhookObjectType,
    WebhookPayload,
)


# ---------------------------------------------------------------------------
# WebhookEvent — strict-input tolerance
# ---------------------------------------------------------------------------


class TestWebhookEventAcceptsUnknownTypes:
    """The model must accept any string for object_type and event_type so
    Smartsheet can add new types without breaking the entire payload."""

    def test_discussion_event_does_not_crash(self):
        """The exact value that crashed production on 2026-05-15."""
        event = WebhookEvent.model_validate({
            "eventType": "created",
            "objectType": "discussion",
            "id": 9876543210987654,
            "rowId": 5555555555,
        })
        assert event.object_type == "discussion"
        assert event.event_type == "created"
        assert event.is_attachment_event is False
        assert event.is_creation_event is True
        # discussion IS in the catalogue, so it is "known"
        assert event.is_known_object_type is True

    def test_proof_event_does_not_crash(self):
        event = WebhookEvent.model_validate({
            "eventType": "updated",
            "objectType": "proof",
            "id": 1111,
        })
        assert event.object_type == "proof"
        assert event.is_attachment_event is False
        assert event.is_known_object_type is True

    def test_reminder_event_does_not_crash(self):
        event = WebhookEvent.model_validate({
            "eventType": "created",
            "objectType": "reminder",
            "id": 2222,
        })
        assert event.object_type == "reminder"
        assert event.is_known_object_type is True

    def test_genuinely_unknown_future_object_type_does_not_crash(self):
        """If Smartsheet adds a brand-new type we have not catalogued,
        the payload still parses. The ``is_known_object_type`` property
        flags it so the operator log line in the route picks it up."""
        event = WebhookEvent.model_validate({
            "eventType": "created",
            "objectType": "someBrandNewTypeFromTheFuture",
            "id": 3333,
        })
        assert event.object_type == "someBrandNewTypeFromTheFuture"
        assert event.is_attachment_event is False
        assert event.is_known_object_type is False

    def test_unknown_event_type_does_not_crash(self):
        """eventType also accepts unknown values (Smartsheet may add new
        verbs besides created/updated/deleted)."""
        event = WebhookEvent.model_validate({
            "eventType": "futureEventTypeNeverSeenBefore",
            "objectType": "attachment",
            "id": 4444,
            "rowId": 100,
        })
        assert event.event_type == "futureEventTypeNeverSeenBefore"
        # Unknown event type means it is not a creation event we should act on
        assert event.is_creation_event is False
        # Still classified as attachment object
        assert event.is_attachment_event is True

    def test_attachment_created_event_is_what_we_process(self):
        event = WebhookEvent.model_validate({
            "eventType": "created",
            "objectType": "attachment",
            "id": 5555,
            "rowId": 200,
        })
        assert event.is_attachment_event is True
        assert event.is_creation_event is True

    def test_attachment_objectcreated_variant(self):
        """Smartsheet uses ``objectCreated`` in some callbacks instead of
        ``created`` — both must count as a creation event."""
        event = WebhookEvent.model_validate({
            "eventType": "objectCreated",
            "objectType": "attachment",
            "id": 6666,
            "rowId": 300,
        })
        assert event.is_creation_event is True
        assert event.is_attachment_event is True


# ---------------------------------------------------------------------------
# WebhookPayload — accepts heterogeneous events
# ---------------------------------------------------------------------------


class TestWebhookPayloadBuildsFromMixedEvents:
    """Defence in depth: even if a code path forgets to pre-filter the
    events list, the model must not crash."""

    def test_payload_with_attachment_plus_discussion_succeeds(self):
        raw_events = [
            {"eventType": "created", "objectType": "attachment", "id": 1, "rowId": 100},
            {"eventType": "created", "objectType": "discussion", "id": 2, "rowId": 100},
        ]
        payload = WebhookPayload(
            webhook_id=999,
            scope="sheet",
            scope_object_id=12345,
            events=raw_events,
        )
        assert len(payload.events) == 2
        assert payload.new_attachment_events  # the property still works
        assert len(payload.new_attachment_events) == 1
        assert payload.new_attachment_events[0].object_type == "attachment"

    def test_payload_with_realistic_mixed_batch_succeeds(self):
        """Reflects the actual production payload shape: attachment events
        mixed with discussion, proof, row, sheet, reminder events."""
        raw_events = [
            {"eventType": "created", "objectType": "attachment", "id": 1, "rowId": 100},
            {"eventType": "updated", "objectType": "row", "id": 100},
            {"eventType": "created", "objectType": "discussion", "id": 2},
            {"eventType": "updated", "objectType": "proof", "id": 3},
            {"eventType": "created", "objectType": "attachment", "id": 4, "rowId": 200},
            {"eventType": "updated", "objectType": "cell", "id": 5},
            {"eventType": "created", "objectType": "reminder", "id": 6},
            {"eventType": "updated", "objectType": "sheet", "id": 7},
            {"eventType": "created", "objectType": "comment", "id": 8},
            {"eventType": "created", "objectType": "attachment", "id": 9, "rowId": 300},
        ]
        payload = WebhookPayload(
            webhook_id=888,
            scope="sheet",
            scope_object_id=55555,
            events=raw_events,
        )
        assert len(payload.events) == 10
        assert len(payload.attachment_events) == 3
        assert len(payload.new_attachment_events) == 3

    def test_payload_with_pre_filtered_attachment_events_only(self):
        """The route now passes ``attachment_events`` (pre-filtered) instead
        of raw events. That path must also keep working."""
        attachment_events = [
            {"eventType": "created", "objectType": "attachment", "id": 1, "rowId": 100},
            {"eventType": "objectCreated", "objectType": "attachment", "id": 2, "rowId": 200},
        ]
        payload = WebhookPayload(
            webhook_id=777,
            scope="sheet",
            scope_object_id=11111,
            events=attachment_events,
        )
        assert len(payload.events) == 2
        assert all(e.is_attachment_event for e in payload.events)
        assert all(e.is_creation_event for e in payload.events)

    def test_payload_with_only_non_attachment_events_succeeds_with_zero_actionable(self):
        """If a batch contains only events we never act on, parsing still
        succeeds and ``new_attachment_events`` is empty."""
        raw_events = [
            {"eventType": "created", "objectType": "discussion", "id": 1},
            {"eventType": "updated", "objectType": "proof", "id": 2},
            {"eventType": "created", "objectType": "reminder", "id": 3},
        ]
        payload = WebhookPayload(
            webhook_id=666,
            scope="sheet",
            scope_object_id=22222,
            events=raw_events,
        )
        assert len(payload.events) == 3
        assert payload.new_attachment_events == []

    def test_payload_with_unknown_future_types_succeeds(self):
        raw_events = [
            {"eventType": "created", "objectType": "attachment", "id": 1, "rowId": 100},
            {"eventType": "weirdNewVerb", "objectType": "weirdNewType", "id": 2},
        ]
        payload = WebhookPayload(
            webhook_id=555,
            scope="sheet",
            scope_object_id=33333,
            events=raw_events,
        )
        assert len(payload.events) == 2
        # The unknown type is parsed but flagged
        unknown_event = next(e for e in payload.events if e.object_type == "weirdNewType")
        assert unknown_event.is_known_object_type is False


# ---------------------------------------------------------------------------
# Enum reference catalogue — still useful as constants
# ---------------------------------------------------------------------------


class TestEnumCatalogueRemainsUsable:
    """The Enum classes are now reference-only but still used in code
    (notably ``is_attachment_event``). Sanity-check the values are stable."""

    def test_attachment_object_type_value(self):
        assert WebhookObjectType.ATTACHMENT.value == "attachment"

    def test_known_object_types_include_the_problematic_ones(self):
        """If this test fails because a member was renamed/removed, the
        route's unknown-types log will start firing for that name. Update
        intentionally."""
        members = {t.value for t in WebhookObjectType}
        for required in ("attachment", "row", "cell", "column", "sheet",
                         "discussion", "proof", "reminder", "comment"):
            assert required in members, f"Catalogue lost {required}"

    def test_known_event_types_include_creation_variants(self):
        members = {t.value for t in WebhookEventType}
        for required in ("created", "objectCreated", "updated", "deleted"):
            assert required in members, f"Catalogue lost {required}"
