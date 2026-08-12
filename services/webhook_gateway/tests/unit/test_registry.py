"""Unit tests for handler registry."""

import pytest

from smarthub_core.platforms.smartsheet.webhook import SmartsheetWebhookHandler

from gateway.routing.registry import HandlerRegistry


def test_default_registry_has_smartsheet():
    registry = HandlerRegistry.default()
    handler = registry.get("smartsheet")
    assert isinstance(handler, SmartsheetWebhookHandler)


def test_unknown_provider_raises():
    registry = HandlerRegistry.default()
    with pytest.raises(KeyError):
        registry.get("stripe")
