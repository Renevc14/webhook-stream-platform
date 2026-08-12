import jwt
import pytest

from jwt_auth import verify_supabase_jwt


def test_verify_supabase_jwt_valid_hs256(monkeypatch):
    secret = "test-secret-key-for-jwt-32chars!!"
    token = jwt.encode(
        {"sub": "user-uuid", "exp": 9_999_999_999},
        secret,
        algorithm="HS256",
    )
    payload = verify_supabase_jwt(f"Bearer {token}", secret)
    assert payload is not None
    assert payload["sub"] == "user-uuid"


def test_verify_supabase_jwt_rejects_bad_sig():
    secret = "a" * 32
    token = jwt.encode({"sub": "x", "exp": 9_999_999_999}, secret, algorithm="HS256")
    assert verify_supabase_jwt(f"Bearer {token}", "different_secret_key_32bytes___") is None


def test_verify_supabase_jwt_missing_header():
    assert verify_supabase_jwt(None, "secret") is None
