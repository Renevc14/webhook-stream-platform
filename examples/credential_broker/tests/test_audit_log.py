import json
from unittest.mock import MagicMock

import audit_log


def test_new_request_id_generates_uuid():
    rid = audit_log.new_request_id(None)
    assert len(rid) == 36


def test_new_request_id_accepts_valid_uuid_header():
    u = "550e8400-e29b-41d4-a716-446655440000"
    assert audit_log.new_request_id(u) == u


def test_record_encrypt_event_stderr_json(capsys):
    sb = MagicMock()
    audit_log.record_encrypt_event(
        sb,
        request_id="r1",
        event_type="test_event",
        outcome="ok",
        http_status=200,
        organization_id="00000000-0000-4000-8000-000000000001",
        meta={"plain_token": "should_be_stripped", "safe": True},
    )
    err = capsys.readouterr().err.strip()
    data = json.loads(err)
    assert data["audit"] == "encrypt_tool"
    assert data["event_type"] == "test_event"
    assert "plain_token" not in data.get("meta", {})
    assert data.get("meta", {}).get("safe") is True
    sb.table.assert_not_called()


def test_persist_called_when_audit_db_enabled(monkeypatch):
    sb = MagicMock()
    monkeypatch.setattr(audit_log, "_AUDIT_DB", True)
    audit_log.record_encrypt_event(
        sb,
        request_id="r2",
        event_type="x",
        outcome="x",
    )
    sb.table.assert_called_once()
    assert sb.table.call_args[0][0] == "encrypt_tool_audit_events"
