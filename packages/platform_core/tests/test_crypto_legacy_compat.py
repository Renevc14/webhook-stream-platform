"""
Cross-module compatibility test: smarthub_core.crypto vs backend/image-to-cell-3 crypto.

This test imports the legacy CryptoService from image-to-cell-3 and verifies that:

1. A ciphertext produced by smarthub_core.crypto can be decrypted by the legacy
   service (when both use the same key).
2. A ciphertext produced by the legacy service can be decrypted by
   smarthub_core.crypto.

If this test passes, the existing tokens stored in `smartsheet_tokens` (encrypted
by image-to-cell-3) will continue to be readable after I2C migrates to
smarthub_core.crypto in Phase 1.
"""

import base64
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: load the legacy CryptoService from backend/image-to-cell-3
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
I2C_ROOT = REPO_ROOT / "backend" / "image-to-cell-3"


@pytest.fixture(scope="module")
def legacy_crypto_class():
    """Import legacy CryptoService from image-to-cell-3 (src.services.crypto)."""
    # The legacy `src.config.settings` module requires these env vars at import
    # time (pydantic Settings validates required fields immediately). Values are
    # placeholders — the legacy crypto class only uses APP_CRYPTO_KEY when the
    # caller does NOT pass an explicit key, and our tests always pass one.
    os.environ.setdefault("SUPABASE_URL", "https://placeholder.supabase.co")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "placeholder")
    os.environ.setdefault("APP_CRYPTO_KEY", base64.b64encode(b"\x00" * 32).decode())

    sys.path.insert(0, str(I2C_ROOT))
    try:
        from src.services.crypto import CryptoService as LegacyCryptoService  # noqa: E402
    finally:
        # Don't pollute sys.path for other tests in the same session
        sys.path.remove(str(I2C_ROOT))

    return LegacyCryptoService


@pytest.fixture
def shared_key_b64() -> str:
    """A fresh AES-256 key, base64-encoded — used by both services in each test."""
    return base64.b64encode(os.urandom(32)).decode()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    def test_smarthub_core_encrypts_legacy_decrypts(self, legacy_crypto_class, shared_key_b64):
        """smarthub_core.crypto produces ciphertexts that legacy can read."""
        from smarthub_core.crypto import CryptoService

        new_svc = CryptoService(key=shared_key_b64)
        legacy_svc = legacy_crypto_class(key=shared_key_b64)

        plaintext = "smartsheet-token-abc123-XYZ"
        ciphertext = new_svc.encrypt(plaintext)
        assert legacy_svc.decrypt(ciphertext) == plaintext

    def test_legacy_encrypts_smarthub_core_decrypts(self, legacy_crypto_class, shared_key_b64):
        """Legacy ciphertexts (e.g., the ones already in smartsheet_tokens) can
        be decrypted by smarthub_core.crypto. This is the load-bearing direction —
        Phase 1 cannot migrate I2C if existing prod tokens become unreadable."""
        from smarthub_core.crypto import CryptoService

        new_svc = CryptoService(key=shared_key_b64)
        legacy_svc = legacy_crypto_class(key=shared_key_b64)

        plaintext = "smartsheet-token-from-existing-prod-row"
        ciphertext = legacy_svc.encrypt(plaintext)
        assert new_svc.decrypt(ciphertext) == plaintext

    def test_unicode_roundtrip_cross_module(self, legacy_crypto_class, shared_key_b64):
        """Non-ASCII content survives a cross-module roundtrip in both directions."""
        from smarthub_core.crypto import CryptoService

        new_svc = CryptoService(key=shared_key_b64)
        legacy_svc = legacy_crypto_class(key=shared_key_b64)

        plaintext = "tókén-✨🔐-with-special-chars-üñ"
        assert legacy_svc.decrypt(new_svc.encrypt(plaintext)) == plaintext
        assert new_svc.decrypt(legacy_svc.encrypt(plaintext)) == plaintext

    def test_different_keys_cross_module_fails(self, legacy_crypto_class):
        """Cross-module decryption with mismatched keys must fail (not silently
        return garbage). Confirms both modules use authenticated encryption."""
        from smarthub_core.crypto import CryptoError, CryptoService

        key_a = base64.b64encode(os.urandom(32)).decode()
        key_b = base64.b64encode(os.urandom(32)).decode()

        new_svc = CryptoService(key=key_a)
        legacy_svc = legacy_crypto_class(key=key_b)

        ciphertext = new_svc.encrypt("does-not-survive")
        # Legacy CryptoError comes from src.services.crypto — we don't import it
        # by name; just assert the call raises.
        with pytest.raises(Exception):
            legacy_svc.decrypt(ciphertext)

        ciphertext_legacy = legacy_svc.encrypt("does-not-survive")
        with pytest.raises(CryptoError):
            new_svc.decrypt(ciphertext_legacy)
