"""
Structured security / error audit for encrypt-tool.

- Emits one JSON object per line to stderr → CloudWatch (treat as immutable stream with retention).
- Optionally persists to ``encrypt_tool_audit_events`` when ENCRYPT_TOOL_AUDIT_TO_DB=1.

Never log plaintext tokens, full ciphertext, or JWT raw values.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_AUDIT_DB = os.getenv("ENCRYPT_TOOL_AUDIT_TO_DB", "").lower() in ("1", "true", "yes")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_audit_line(record: Dict[str, Any]) -> None:
    """Append-only style log line (immutable once shipped to CloudWatch with retention)."""
    payload = {
        "audit": "encrypt_tool",
        "ts": _now_iso(),
        **record,
    }
    print(json.dumps(payload, default=str), file=sys.stderr, flush=True)


def persist_audit_row(sb, row: Dict[str, Any]) -> None:
    if not _AUDIT_DB or sb is None:
        return
    try:
        sb.table("encrypt_tool_audit_events").insert(row).execute()
    except Exception:
        # Never fail the request because audit insert failed; stderr already has the line.
        emit_audit_line(
            {
                "event_type": "audit_persist_failed",
                "outcome": "error",
                "error_code": "AUDIT_INSERT",
                "meta": {"hint": str(row.get("event_type", ""))},
            }
        )


def record_encrypt_event(
    sb,
    *,
    request_id: str,
    event_type: str,
    outcome: str,
    http_status: Optional[int] = None,
    organization_id: Optional[str] = None,
    key_kind: Optional[str] = None,
    slug: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    error_code: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    meta = dict(meta or {})
    # Hard guard: strip common leak keys if callers pass a sloppy meta
    for k in list(meta.keys()):
        if k.lower() in ("plain_token", "encrypted_token", "authorization", "api_key"):
            meta.pop(k, None)

    line = {
        "event_type": event_type,
        "outcome": outcome,
        "http_status": http_status,
        "organization_id": organization_id,
        "key_kind": key_kind,
        "slug": slug,
        "actor_user_id": actor_user_id,
        "request_id": request_id,
        "error_code": error_code,
        "meta": meta,
    }
    emit_audit_line(line)

    db_row = {
        "event_type": event_type,
        "outcome": outcome,
        "http_status": http_status,
        "organization_id": organization_id,
        "key_kind": key_kind,
        "slug": slug,
        "actor_user_id": actor_user_id,
        "request_id": request_id,
        "error_code": error_code,
        "meta": meta,
    }
    persist_audit_row(sb, db_row)


def new_request_id(header_value: Optional[str]) -> str:
    if header_value and str(header_value).strip():
        try:
            uuid.UUID(str(header_value).strip())
            return str(header_value).strip()
        except (ValueError, TypeError):
            pass
    return str(uuid.uuid4())
