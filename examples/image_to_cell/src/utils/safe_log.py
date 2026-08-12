"""
Safe Logging Utilities
=====================

Redacts PII and sensitive data from log messages for penetration testing
and enterprise readiness. Aligned with BAU create_safe_log_data pattern.
"""

import re
from typing import Any, Dict, List, Optional

# Keys that contain PII or sensitive data - redact entirely
SENSITIVE_KEYS = frozenset({
    "user_id", "organization_id", "org_id", "config_id", "token", "password",
    "key", "secret", "credential", "auth", "shared_secret", "api_key",
    "access_token", "refresh_token", "authorization",
})

# UUID pattern for detecting UUIDs in string values
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _is_sensitive_key(key: str) -> bool:
    """Check if a key name indicates sensitive data."""
    k = key.lower()
    return any(sk in k for sk in SENSITIVE_KEYS)


def redact(value: Any) -> str:
    """
    Redact a value for safe logging.

    - UUIDs and sensitive-looking strings -> [REDACTED]
    - None -> [none]
    - Other -> str(value)
    """
    if value is None:
        return "[none]"
    s = str(value).strip()
    if not s:
        return "[empty]"
    if UUID_PATTERN.match(s):
        return "[REDACTED]"
    # Redact if it looks like a token/secret (long alphanumeric)
    if len(s) > 20 and re.match(r"^[a-zA-Z0-9_-]+$", s):
        return "[REDACTED]"
    return s


def create_safe_log_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a safe version of a dict for logging by redacting sensitive keys.

    Recursively processes nested dicts and lists of dicts.
    """
    if not isinstance(data, dict):
        return data

    safe = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            safe[key] = "[REDACTED]"
        elif isinstance(value, dict):
            safe[key] = create_safe_log_data(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            safe[key] = [create_safe_log_data(item) for item in value]
        else:
            safe[key] = value
    return safe


def safe_exception_message(exc: Exception) -> str:
    """
    Return a safe representation of an exception for logging.

    Avoids leaking connection strings, tokens, or other secrets
    that might be in the exception message.
    """
    msg = str(exc)
    if not msg:
        return type(exc).__name__
    # Redact common secret patterns
    if "password" in msg.lower() or "token" in msg.lower() or "secret" in msg.lower():
        return f"{type(exc).__name__}: [REDACTED - may contain secrets]"
    # Truncate very long messages (could contain dumps)
    if len(msg) > 200:
        return f"{type(exc).__name__}: {msg[:200]}..."
    return f"{type(exc).__name__}: {msg}"
