"""
LOCAL / BREAK-GLASS ONLY — do not run in shared shells, CI logs, or production.

This script decrypts or encrypts sample values and may print **plaintext API keys**
to stdout. Set ENCRYPT_DEBUG_PRINT_PLAINTEXT=1 to echo decrypted or test-encrypted
secrets; otherwise only lengths and success/failure are shown.
"""

import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _may_print_plaintext(label: str, value: str) -> None:
    if os.environ.get("ENCRYPT_DEBUG_PRINT_PLAINTEXT", "").lower() in ("1", "true", "yes"):
        print(f"{label}{value}")
    else:
        print(f"{label}<redacted, len={len(value)}> — set ENCRYPT_DEBUG_PRINT_PLAINTEXT=1 to echo")


def print_key_info():
    key = os.environ.get("APP_CRYPTO_KEY")
    if not key:
        print("❌ APP_CRYPTO_KEY is NOT set in environment variables.")
        return None
    try:
        decoded = base64.b64decode(key, validate=True)
        print(f"✅ APP_CRYPTO_KEY loaded. Length: {len(decoded)} bytes (should be 32)")
        return decoded
    except Exception as e:
        print(f"❌ APP_CRYPTO_KEY could not be base64-decoded: {e}")
        return None


def decrypt_token(b64_ct, key):
    try:
        blob = base64.b64decode(b64_ct)
        iv, ct = blob[:12], blob[12:]
        aead = AESGCM(key)
        decrypted = aead.decrypt(iv, ct, None).decode()
        print("✅ Decryption successful")
        _may_print_plaintext("   Plaintext: ", decrypted)
    except Exception as e:
        print(f"❌ Decryption failed: {e}")


def encrypt_token(plain, key):
    try:
        iv = os.urandom(12)
        aead = AESGCM(key)
        ct = aead.encrypt(iv, plain.encode(), None)
        blob = iv + ct
        b64 = base64.b64encode(blob).decode()
        print("✅ Encryption successful")
        _may_print_plaintext("   Ciphertext (b64): ", b64)
        return b64
    except Exception as e:
        print(f"❌ Encryption failed: {e}")


if __name__ == "__main__":
    print("=== APP_CRYPTO_KEY Debug (local use only) ===")
    key = print_key_info()
    if not key:
        exit(1)

    # Test decryption
    encrypted = input("Paste encrypted value from Supabase (or leave blank to skip): ").strip()
    if encrypted:
        decrypt_token(encrypted, key)

    # Test encryption
    plain = input("Enter a plain API key to encrypt (or leave blank to skip): ").strip()
    if plain:
        encrypt_token(plain, key)
