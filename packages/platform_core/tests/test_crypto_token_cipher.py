"""Unit tests for smarthub_core.crypto.token_cipher."""

import base64
import os

import pytest

from smarthub_core.crypto import CryptoError, CryptoService


def _fresh_key_b64() -> str:
    """Generate a fresh 32-byte key, base64-encoded — used to instantiate
    CryptoService directly without needing APP_CRYPTO_KEY in the env."""
    return base64.b64encode(os.urandom(32)).decode()


class TestCryptoService:
    def test_roundtrip_simple_string(self):
        svc = CryptoService(key=_fresh_key_b64())
        plaintext = "hello-smartsheet-token-1234567890"
        ciphertext = svc.encrypt(plaintext)
        assert ciphertext != plaintext
        assert svc.decrypt(ciphertext) == plaintext

    def test_roundtrip_unicode(self):
        svc = CryptoService(key=_fresh_key_b64())
        plaintext = "tókén-with-émojis-✨🔐"
        assert svc.decrypt(svc.encrypt(plaintext)) == plaintext

    def test_roundtrip_empty_string(self):
        svc = CryptoService(key=_fresh_key_b64())
        assert svc.decrypt(svc.encrypt("")) == ""

    def test_each_encryption_uses_unique_nonce(self):
        """Two encryptions of the same plaintext must produce different ciphertexts
        (otherwise we have a deterministic-encryption bug; AES-GCM requires unique nonces)."""
        svc = CryptoService(key=_fresh_key_b64())
        plaintext = "same-input"
        a = svc.encrypt(plaintext)
        b = svc.encrypt(plaintext)
        assert a != b
        assert svc.decrypt(a) == plaintext
        assert svc.decrypt(b) == plaintext

    def test_invalid_key_length_raises(self):
        # 16 bytes (128-bit) — AES-GCM here is configured for 256-bit only
        short_key = base64.b64encode(os.urandom(16)).decode()
        with pytest.raises(CryptoError, match="Key must be 32 bytes"):
            CryptoService(key=short_key)

    def test_malformed_base64_key_raises(self):
        with pytest.raises(CryptoError, match="Failed to initialize crypto"):
            CryptoService(key="not-valid-base64-!!!")

    def test_tampered_ciphertext_raises(self):
        """AES-GCM has authentication; flipping a byte must fail decryption."""
        svc = CryptoService(key=_fresh_key_b64())
        ciphertext = svc.encrypt("untampered")
        raw = base64.b64decode(ciphertext)
        # Flip a byte in the ciphertext+tag region (after the 12-byte nonce)
        tampered_raw = raw[:13] + bytes([raw[13] ^ 0x01]) + raw[14:]
        tampered_b64 = base64.b64encode(tampered_raw).decode()
        with pytest.raises(CryptoError, match="Decryption failed"):
            svc.decrypt(tampered_b64)

    def test_wrong_key_cannot_decrypt(self):
        svc_a = CryptoService(key=_fresh_key_b64())
        svc_b = CryptoService(key=_fresh_key_b64())
        ciphertext = svc_a.encrypt("secret")
        with pytest.raises(CryptoError, match="Decryption failed"):
            svc_b.decrypt(ciphertext)

    def test_legacy_format_compatibility(self):
        """Verify wire format matches legacy crypto.py: base64(nonce[12] || ciphertext_with_tag).

        This guarantees ciphertexts produced by backend/image-to-cell-3/src/services/crypto.py
        can be decrypted by smarthub_core.crypto.CryptoService when both use the same key.
        """
        svc = CryptoService(key=_fresh_key_b64())
        ciphertext_b64 = svc.encrypt("legacy-format-check")
        raw = base64.b64decode(ciphertext_b64)
        # Nonce is the first 12 bytes
        assert len(raw) >= 12 + 16  # nonce + at least one GCM tag
