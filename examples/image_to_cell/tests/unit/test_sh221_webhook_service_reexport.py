"""SH-221 — SmartsheetWebhookService re-export identity (PR #199 compatibility)."""

from smarthub_core.platforms.smartsheet import (
    SmartsheetWebhookService as CoreSmartsheetWebhookService,
)

from src.services.smartsheet.webhooks import SmartsheetWebhookService


def test_i2c_webhook_service_is_core_reexport():
    assert SmartsheetWebhookService is CoreSmartsheetWebhookService
