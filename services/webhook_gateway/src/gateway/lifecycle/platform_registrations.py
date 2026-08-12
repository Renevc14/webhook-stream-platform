"""SH-230 — platform_webhook_registrations helpers.

Canonical implementation lives in ``smarthub_core.webhooks.platform_registrations``.
This module re-exports for gateway imports that already target
``gateway.lifecycle.platform_registrations``.

Write semantics (SH-230 D): platform is the authoritative write SoT when
``PLATFORM_WEBHOOK_READ`` is enabled. Tool ``*_webhooks`` rows are a soft-fail
mirror until a follow-up retires them. Prefer
``upsert_platform_registration(..., soft_fail=False)`` (default) for required
platform writes; pass ``soft_fail=True`` only for transitional soft mirrors.
"""

from __future__ import annotations

from smarthub_core.webhooks.platform_registrations import (  # noqa: F401
    PLATFORM_WEBHOOKS_TABLE,
    PlatformRegistrationError,
    count_platform_vs_tool_parity,
    delete_platform_registration,
    delete_platform_registration_by_worksheet,
    upsert_platform_registration,
)

__all__ = [
    "PLATFORM_WEBHOOKS_TABLE",
    "PlatformRegistrationError",
    "count_platform_vs_tool_parity",
    "delete_platform_registration",
    "delete_platform_registration_by_worksheet",
    "upsert_platform_registration",
]
