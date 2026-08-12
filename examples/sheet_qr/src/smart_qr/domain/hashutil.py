"""Stable payload hashing."""

from __future__ import annotations

import hashlib


def payload_hash(text: str, code_type: str) -> str:
    raw = f"{code_type}\n{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
