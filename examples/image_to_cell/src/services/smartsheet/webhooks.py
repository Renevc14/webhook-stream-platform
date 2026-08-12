"""
Smartsheet Webhook Service
==========================

Backwards-compatible re-export of the shared
``smarthub_core.platforms.smartsheet.SmartsheetWebhookService`` (SH-207 /
SH-193). Call sites under I2C keep importing from this module; the
implementation lives in smarthub_core so the Webhook Gateway lifecycle
manager can use the same CRUD without duplicating Smartsheet SDK wrapping.
"""

from __future__ import annotations

from smarthub_core.platforms.smartsheet.webhook_service import (  # noqa: F401
    SHARED_SECRET_RETRIES,
    SHARED_SECRET_RETRY_DELAY_S,
    SmartsheetWebhook,
    SmartsheetWebhookService,
    WebhookStatus,
)

__all__ = [
    "SHARED_SECRET_RETRIES",
    "SHARED_SECRET_RETRY_DELAY_S",
    "SmartsheetWebhook",
    "SmartsheetWebhookService",
    "WebhookStatus",
]
