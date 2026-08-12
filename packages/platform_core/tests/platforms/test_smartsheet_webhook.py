"""Unit tests for SmartsheetWebhookHandler.

Covers the three responsibilities of the handler:
- Challenge handshake (Smartsheet-Hook-Challenge header echoes back)
- HMAC SHA-256 signature verification (including the 63-char quirk)
- Event extraction into NormalizedEvent
"""

import hashlib
import hmac
import json

import pytest

from smarthub_core.platforms.shared.webhook_base import (
    ChallengeResponse,
    NormalizedEvent,
    WebhookSignatureError,
)
from smarthub_core.platforms.smartsheet.webhook import (
    CHALLENGE_HEADER,
    CHALLENGE_RESPONSE_HEADER,
    SIGNATURE_HEADER,
    SmartsheetWebhookHandler,
)


SECRET = "test-shared-secret-1234567890"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture
def handler():
    return SmartsheetWebhookHandler()


class TestProvider:
    def test_provider_slug(self, handler):
        assert handler.provider == "smartsheet"


class TestChallenge:
    def test_returns_challenge_response_when_header_present(self, handler):
        challenge_value = "abc123-challenge"
        resp = handler.handle_challenge(
            headers={CHALLENGE_HEADER: challenge_value},
            body=b"",
        )
        assert isinstance(resp, ChallengeResponse)
        assert resp.body == challenge_value.encode()
        assert resp.status_code == 200
        assert resp.headers[CHALLENGE_RESPONSE_HEADER] == challenge_value

    def test_returns_none_when_no_challenge_header(self, handler):
        resp = handler.handle_challenge(headers={}, body=b'{"events":[]}')
        assert resp is None

    def test_case_insensitive_header_lookup(self, handler):
        resp = handler.handle_challenge(
            headers={"smartsheet-hook-challenge": "x"}, body=b""
        )
        assert resp is not None
        assert resp.body == b"x"

    def test_alt_x_prefixed_header(self, handler):
        resp = handler.handle_challenge(
            headers={"X-Smartsheet-Hook-Challenge": "y"}, body=b""
        )
        assert resp is not None
        assert resp.body == b"y"


class TestSignature:
    def test_valid_signature_passes(self, handler):
        body = b'{"events":[]}'
        sig = _sign(body)
        assert handler.verify_signature({SIGNATURE_HEADER: sig}, body, SECRET)

    def test_constant_time_compare_lowercased(self, handler):
        body = b'{"events":[]}'
        sig = _sign(body).upper()  # Some clients send uppercase
        assert handler.verify_signature({SIGNATURE_HEADER: sig}, body, SECRET)

    def test_63_char_signature_padded(self, handler):
        body = b'{"events":[]}'
        full = _sign(body)
        # Force a 63-char signature by stripping a leading zero if present;
        # otherwise prepend a non-zero so the strip simulates the bug.
        if full[0] == "0":
            stripped = full[1:]
        else:
            # Construct a body whose hex starts with 0
            body = b"x"
            full_x = _sign(body)
            if full_x[0] == "0":
                stripped = full_x[1:]
            else:
                pytest.skip("could not construct a body with leading-zero hex")

        assert len(stripped) == 63
        assert handler.verify_signature(
            {SIGNATURE_HEADER: stripped}, body, SECRET
        )

    def test_missing_header_raises(self, handler):
        with pytest.raises(WebhookSignatureError, match="missing"):
            handler.verify_signature({}, b"x", SECRET)

    def test_empty_secret_raises(self, handler):
        with pytest.raises(WebhookSignatureError, match="no shared secret"):
            handler.verify_signature(
                {SIGNATURE_HEADER: "a" * 64}, b"x", ""
            )

    def test_wrong_length_raises(self, handler):
        with pytest.raises(WebhookSignatureError, match="length"):
            handler.verify_signature(
                {SIGNATURE_HEADER: "abcd"}, b"x", SECRET
            )

    def test_signature_mismatch_raises(self, handler):
        body = b'{"events":[]}'
        wrong = _sign(body, secret="wrong-secret")
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            handler.verify_signature(
                {SIGNATURE_HEADER: wrong}, body, SECRET
            )

    def test_body_tamper_detected(self, handler):
        body = b'{"events":[]}'
        sig = _sign(body)
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            handler.verify_signature(
                {SIGNATURE_HEADER: sig}, b'{"events":[{"x":1}]}', SECRET
            )


class TestExtractEvents:
    def test_attachment_created(self, handler):
        body = json.dumps(
            {
                "webhookId": 1,
                "events": [
                    {
                        "objectType": "attachment",
                        "eventType": "created",
                        "id": 555,
                        "rowId": 999,
                        "timestamp": "2026-05-06T10:00:00Z",
                    }
                ],
            }
        ).encode()

        events = handler.extract_events({}, body)
        assert len(events) == 1
        assert isinstance(events[0], NormalizedEvent)
        assert events[0].event_type == "smartsheet.attachment.created"
        assert events[0].external_id == "555"
        assert events[0].occurred_at == "2026-05-06T10:00:00Z"
        assert events[0].raw["rowId"] == 999

    def test_multiple_events_emitted_individually(self, handler):
        body = json.dumps(
            {
                "events": [
                    {"objectType": "attachment", "eventType": "created", "id": 1},
                    {"objectType": "row", "eventType": "updated", "id": 2},
                    {"objectType": "discussion", "eventType": "created", "id": 3},
                ]
            }
        ).encode()
        events = handler.extract_events({}, body)
        assert [e.event_type for e in events] == [
            "smartsheet.attachment.created",
            "smartsheet.row.updated",
            "smartsheet.discussion.created",
        ]

    def test_empty_events_returns_empty_list(self, handler):
        body = json.dumps({"events": []}).encode()
        assert handler.extract_events({}, body) == []

    def test_missing_events_field_returns_empty_list(self, handler):
        body = b"{}"
        assert handler.extract_events({}, body) == []

    def test_malformed_body_raises_signature_error(self, handler):
        with pytest.raises(WebhookSignatureError, match="malformed JSON"):
            handler.extract_events({}, b"not-json")

    def test_non_dict_event_skipped(self, handler):
        body = json.dumps({"events": ["string-instead-of-dict", 42]}).encode()
        assert handler.extract_events({}, body) == []

    def test_unknown_event_type_normalized(self, handler):
        body = json.dumps(
            {"events": [{"objectType": None, "eventType": None, "id": 1}]}
        ).encode()
        events = handler.extract_events({}, body)
        assert events[0].event_type == "smartsheet.unknown.unknown"
