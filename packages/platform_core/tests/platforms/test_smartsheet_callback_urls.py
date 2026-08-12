"""Tests for shared Smartsheet gateway callback URL helpers (SH-192)."""

from smarthub_core.platforms.smartsheet.callback_urls import (
    gateway_callback_url,
    is_our_webhook_callback,
    resolve_webhook_callback_url,
    webhook_callback_ownership_patterns,
)

ORG = "78e2278d-ffaa-4299-a969-9424275cc207"
SLUG = "smart-image-to-cell"
BASE = "https://webhooks.prodactivelabs.com"
LEGACY = "https://smart-image-to-cell.prodactivelabs.com/webhook/smartsheet"
GATEWAY_ORG = f"{BASE}/webhook/smartsheet/{SLUG}/{ORG}"


def test_gateway_callback_url_shape():
    assert gateway_callback_url(BASE + "/", SLUG, ORG) == GATEWAY_ORG


def test_resolve_prefers_gateway_base():
    assert (
        resolve_webhook_callback_url(
            organization_id=ORG,
            gateway_base_url=BASE,
            legacy_callback_url=LEGACY,
            product_slug=SLUG,
        )
        == GATEWAY_ORG
    )


def test_resolve_legacy_when_no_gateway():
    assert (
        resolve_webhook_callback_url(
            organization_id=ORG,
            gateway_base_url=None,
            legacy_callback_url=LEGACY,
            product_slug=SLUG,
        )
        == LEGACY
    )


def test_ownership_patterns_include_gateway_and_legacy():
    patterns = webhook_callback_ownership_patterns(
        gateway_base_url=BASE,
        legacy_callback_url=LEGACY,
        product_slug=SLUG,
    )
    assert f"{BASE}/webhook/smartsheet/{SLUG}/" in patterns
    assert LEGACY in patterns
    assert is_our_webhook_callback(GATEWAY_ORG, patterns)
    assert is_our_webhook_callback(LEGACY, patterns)
    assert not is_our_webhook_callback("https://other.example/hook", patterns)


def test_gateway_callback_url_provider_override_sh231():
    assert (
        gateway_callback_url(BASE, "demo-tool", ORG, provider="hubspot")
        == f"{BASE}/webhook/hubspot/demo-tool/{ORG}"
    )
    patterns = webhook_callback_ownership_patterns(
        gateway_base_url=BASE,
        legacy_callback_url=None,
        product_slug="demo-tool",
        provider="hubspot",
    )
    assert patterns == [f"{BASE}/webhook/hubspot/demo-tool/"]
    # Default provider remains smartsheet (backward compatible).
    assert gateway_callback_url(BASE, SLUG, ORG) == GATEWAY_ORG
