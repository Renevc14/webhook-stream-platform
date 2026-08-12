"""
AES-GCM encryption/decryption for Smartsheet tokens (and any other secret).

Behavior is bit-for-bit compatible with the legacy `crypto_helper.py` and
`backend/image-to-cell-3/src/services/crypto.py` so existing ciphertexts in
Postgres can be decrypted unchanged.

Wire format: base64(nonce[12] || ciphertext_with_tag).
"""

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from smarthub_core.settings import CoreSettings, get_core_settings


class CryptoError(Exception):
    """Raised when encryption or decryption fails."""


class CryptoService:
    """AES-256-GCM authenticated encryption.

    The key is a base64-encoded 32-byte value (256 bits). Same `APP_CRYPTO_KEY`
    used by every Smarthub backend — rotating it requires a coordinated rotate
    of all stored ciphertexts; do not change in isolation.
    """

    _NONCE_BYTES = 12

    def __init__(self, key: Optional[str] = None, *, settings: Optional[CoreSettings] = None):
        if key is None:
            key = (settings or get_core_settings()).app_crypto_key

        try:
            self._key = base64.b64decode(key)
            if len(self._key) != 32:
                raise CryptoError(f"Key must be 32 bytes, got {len(self._key)}")
            self._aesgcm = AESGCM(self._key)
        except CryptoError:
            raise
        except Exception as e:
            raise CryptoError(f"Failed to initialize crypto: {e}") from e

    def encrypt(self, plaintext: str) -> str:
        try:
            nonce = os.urandom(self._NONCE_BYTES)
            ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode(), None)
            return base64.b64encode(nonce + ciphertext).decode()
        except Exception as e:
            raise CryptoError(f"Encryption failed: {e}") from e

    def decrypt(self, ciphertext_b64: str) -> str:
        try:
            data = base64.b64decode(ciphertext_b64)
            nonce = data[: self._NONCE_BYTES]
            ciphertext = data[self._NONCE_BYTES :]
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode()
        except Exception as e:
            raise CryptoError(f"Decryption failed: {e}") from e


_crypto_service: Optional[CryptoService] = None


def get_crypto_service() -> CryptoService:
    """Lazy singleton — created on first call using CoreSettings."""
    global _crypto_service
    if _crypto_service is None:
        _crypto_service = CryptoService()
    return _crypto_service


def encrypt_token(token: str) -> str:
    return get_crypto_service().encrypt(token)


def decrypt_token(encrypted_token: str) -> str:
    return get_crypto_service().decrypt(encrypted_token)
