"""Unit tests for Supabase JWT helper used by I2C."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from src.api.jwt_auth import verify_supabase_jwt


JWT_SECRET = "unit-test-jwt-secret-32chars!!"


def test_verify_supabase_jwt_accepts_valid_bearer():
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    token = jwt.encode({"sub": "user-abc", "exp": exp}, JWT_SECRET, algorithm="HS256")
    payload = verify_supabase_jwt(f"Bearer {token}", JWT_SECRET)
    assert payload is not None
    assert payload.get("sub") == "user-abc"


def test_verify_supabase_jwt_rejects_wrong_secret():
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    token = jwt.encode({"sub": "user-abc", "exp": exp}, JWT_SECRET, algorithm="HS256")
    assert verify_supabase_jwt(f"Bearer {token}", "wrong-secret-32-chars-minimum!!") is None


def test_verify_supabase_jwt_rejects_expired():
    exp = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    token = jwt.encode({"sub": "user-abc", "exp": exp}, JWT_SECRET, algorithm="HS256")
    assert verify_supabase_jwt(f"Bearer {token}", JWT_SECRET) is None


@pytest.mark.parametrize("header", [None, "", "Bearer ", "Basic xxx"])
def test_verify_supabase_jwt_rejects_bad_header(header):
    assert verify_supabase_jwt(header, JWT_SECRET) is None
