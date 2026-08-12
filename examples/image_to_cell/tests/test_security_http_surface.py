"""
HTTP security surface checks aligned with production ALB + hub behavior.

These mirror what you see in CloudWatch: GET /health → 200, GET / → 200,
POST / → 405 (root is GET-only), hub routes without Bearer → 401 when
SUPABASE_JWT_SECRET is set in production, DLQ still gated by X-Admin-Key.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.dependencies import get_backfill_worker_dep
from src.api.routes.dlq import get_dlq_repo

JWT_SECRET = "unit-test-jwt-secret-32chars!!"

_BASE_ENV: Dict[str, str] = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "APP_CRYPTO_KEY": "0123456789abcdef0123456789abcdef",
    "WEBHOOK_LIFECYCLE_ENABLED": "false",
    "DLQ_PROCESSOR_ENABLED": "false",
    "ENABLE_RATE_LIMITING": "false",
}


def _make_app(monkeypatch: pytest.MonkeyPatch, **env: Any):
    for k, v in {**_BASE_ENV, **env}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    from src.config.settings import get_settings

    get_settings.cache_clear()
    from src.api.app import create_app

    return create_app()


def _bearer(sub: str = "user-abc") -> str:
    exp = int(time.time()) + 3600
    token = jwt.encode({"sub": sub, "exp": exp}, JWT_SECRET, algorithm="HS256")
    return f"Bearer {token}"


class _FakeBackfillWorker:
    def run_for_worksheet(self, worksheet_id: int, max_pages=None) -> None:
        return None


def test_alb_style_health_and_root_post_is_405(monkeypatch):
    """Matches ELB + container probes: /health 200; POST / is not a webhook route."""
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    with TestClient(app) as client:
        h = client.get("/health")
        assert h.status_code == 200
        assert h.json().get("status") == "healthy"

        root = client.get("/")
        assert root.status_code == 200

        bad = client.post("/")
        assert bad.status_code == 405


def test_security_headers_on_response(monkeypatch):
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_hub_route_401_without_bearer(monkeypatch):
    """Same class of response as POST /configs/backfill without session in prod."""
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    with TestClient(app) as client:
        r = client.post("/configs/backfill", json={"worksheet_id": 1})
        assert r.status_code == 401
        assert "session token" in (r.json().get("detail") or "").lower()


def test_options_preflight_passes_without_bearer(monkeypatch):
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    with TestClient(app) as client:
        r = client.options(
            "/configs/backfill",
            headers={
                "Origin": "https://app.prodactivelabs.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 200


def test_production_missing_jwt_secret_fail_closed(monkeypatch):
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    from src.config.settings import get_settings

    get_settings.cache_clear()
    from src.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.post("/configs/backfill", json={"worksheet_id": 1})
        assert r.status_code == 503
        assert "SUPABASE_JWT_SECRET" in (r.json().get("detail") or "")


def test_backfill_200_with_valid_bearer_and_org(monkeypatch):
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    app.dependency_overrides[get_backfill_worker_dep] = lambda: _FakeBackfillWorker()
    try:
        with TestClient(app) as client:
            r = client.post(
                "/configs/backfill",
                json={"worksheet_id": 1},
                headers={
                    "Authorization": _bearer("user-1"),
                    "X-Organization-ID": "00000000-0000-0000-0000-000000000001",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body.get("status") == "started" or "worksheet" in body
    finally:
        app.dependency_overrides.clear()


def test_x_user_id_must_match_jwt_sub(monkeypatch):
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
    )
    app.dependency_overrides[get_backfill_worker_dep] = lambda: _FakeBackfillWorker()
    try:
        with TestClient(app) as client:
            r = client.post(
                "/configs/backfill",
                json={"worksheet_id": 1},
                headers={
                    "Authorization": _bearer("user-1"),
                    "X-User-ID": "user-2",
                    "X-Organization-ID": "00000000-0000-0000-0000-000000000001",
                },
            )
            assert r.status_code == 403
            assert "match" in (r.json().get("detail") or "").lower()
    finally:
        app.dependency_overrides.clear()


class _FakeDlqRepo:
    def get_stats(self) -> dict:
        return {"pending": 0}


def test_dlq_requires_admin_key_despite_jwt_exempt_prefix(monkeypatch):
    """JWT gate skips /dlq/*; routes still enforce I2C_ADMIN_API_KEY."""
    app = _make_app(
        monkeypatch,
        ENVIRONMENT="production",
        SUPABASE_JWT_SECRET=JWT_SECRET,
        I2C_ADMIN_API_KEY="super-secret-admin-key-for-tests",
    )
    app.dependency_overrides[get_dlq_repo] = lambda: _FakeDlqRepo()
    try:
        with TestClient(app) as client:
            r = client.get("/dlq/stats")
            assert r.status_code == 401
            assert "admin" in (r.json().get("detail") or "").lower()

            ok = client.get(
                "/dlq/stats",
                headers={"X-Admin-Key": "super-secret-admin-key-for-tests"},
            )
            assert ok.status_code == 200
            assert "stats" in ok.json()
    finally:
        app.dependency_overrides.clear()
