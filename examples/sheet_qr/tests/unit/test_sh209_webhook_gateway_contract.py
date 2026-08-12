"""SH-209 / SH-215 — SmartQR webhook secrets and gateway callback URL."""

from __future__ import annotations

import base64
from uuid import UUID

import pytest
from smarthub_core.crypto import CryptoService

from smart_qr.application.webhook_lifecycle import (
    encrypt_webhook_secret,
    resolve_smartqr_webhook_callback_url,
)
from smart_qr.config.settings import Settings


@pytest.fixture
def crypto_key_b64() -> str:
    return base64.b64encode(b"\x42" * 32).decode()


def test_secret_is_encrypted_with_the_platform_cipher(crypto_key_b64):
    """The gateway decrypts this column with the shared APP_CRYPTO_KEY, so the
    wire format has to be plain smarthub_core AES-GCM."""
    secret = "ss-webhook-shared-secret-xyz"

    enc = encrypt_webhook_secret(secret, crypto_key_b64)

    assert enc != secret
    assert CryptoService(key=crypto_key_b64).decrypt(enc) == secret


def test_callback_url_includes_product_slug():
    settings = Settings(
        smartsheet_webhook_callback_base_url="https://webhooks.example.com",
        product_slug="smart-qr",
    )
    org = UUID("00000000-0000-0000-0000-0000000000aa")

    url = resolve_smartqr_webhook_callback_url(settings, org)

    assert url == (
        "https://webhooks.example.com/webhook/smartsheet/smart-qr/"
        "00000000-0000-0000-0000-0000000000aa"
    )


def test_callback_url_accepts_a_tunnel_base():
    """Local dev points the tunnel at the gateway, not at SmartQR."""
    settings = Settings(
        smartsheet_webhook_callback_base_url="https://xxxx.ngrok-free.app",
        product_slug="smart-qr",
    )

    url = resolve_smartqr_webhook_callback_url(settings, "org-xyz")

    assert url == "https://xxxx.ngrok-free.app/webhook/smartsheet/smart-qr/org-xyz"


def test_callback_url_requires_a_gateway_base():
    """Registering without a gateway base would point Smartsheet at nothing."""
    settings = Settings(smartsheet_webhook_callback_base_url="")

    with pytest.raises(ValueError, match="SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL"):
        resolve_smartqr_webhook_callback_url(settings, "org-1")
