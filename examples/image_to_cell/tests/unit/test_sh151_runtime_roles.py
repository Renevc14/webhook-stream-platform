"""SH-151 / SH-211 — runtime role flags and APP_MODE alias."""

import pytest

from src.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _minimal_env(**overrides) -> dict:
    base = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        "APP_CRYPTO_KEY": "0" * 64,
    }
    base.update(overrides)
    return base


def test_defaults_monolith_all_roles_enabled(monkeypatch):
    for key, val in _minimal_env().items():
        monkeypatch.setenv(key, val)
    for var in (
        "APP_MODE",
        "RUN_HTTP_API",
        "RUN_WEBHOOK_RECEIVER",
        "RUN_STREAM_CONSUMER",
        "RUN_LIFECYCLE_POLLER",
    ):
        monkeypatch.delenv(var, raising=False)

    s = Settings()
    assert s.run_http_api is True
    assert s.run_webhook_receiver is True
    assert s.run_stream_consumer is True
    assert s.run_lifecycle_poller is True
    assert "webhook_receiver=True" in s.runtime_roles_summary


def test_app_mode_api_disables_stream_consumer(monkeypatch):
    env = _minimal_env(APP_MODE="api")
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    s = Settings()
    assert s.run_http_api is True
    assert s.run_stream_consumer is False
    assert s.run_lifecycle_poller is True


def test_app_mode_worker_i2c_api_only_consumer(monkeypatch):
    env = _minimal_env(APP_MODE="worker-i2c")
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    s = Settings()
    assert s.run_http_api is False
    assert s.run_stream_consumer is True
    assert s.run_lifecycle_poller is False


def test_explicit_flags_override_without_app_mode(monkeypatch):
    env = _minimal_env(
        RUN_HTTP_API="false",
        RUN_STREAM_CONSUMER="true",
        RUN_LIFECYCLE_POLLER="false",
    )
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    s = Settings()
    assert s.run_http_api is False
    assert s.run_stream_consumer is True
    assert s.run_lifecycle_poller is False


def test_run_webhook_receiver_false_keeps_http_api(monkeypatch):
    """SH-211: gateway cutover disables direct receiver, keeps Hub API."""
    env = _minimal_env(
        RUN_HTTP_API="true",
        RUN_WEBHOOK_RECEIVER="false",
        RUN_STREAM_CONSUMER="true",
        RUN_LIFECYCLE_POLLER="false",
    )
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("APP_MODE", raising=False)

    s = Settings()
    assert s.run_http_api is True
    assert s.run_webhook_receiver is False


def test_invalid_app_mode_raises(monkeypatch):
    env = _minimal_env(APP_MODE="invalid")
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    with pytest.raises(ValueError, match="Invalid APP_MODE"):
        Settings()


def _openapi_paths(app) -> set[str]:
    """OpenAPI paths flatten included routers (app.routes alone hides them)."""
    return set(app.openapi().get("paths", {}).keys())


def test_create_app_omits_webhook_when_receiver_disabled(monkeypatch):
    """SH-211: /webhook/smartsheet gone; configs + health remain."""
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        "APP_CRYPTO_KEY": "0" * 64,
        "WEBHOOK_LIFECYCLE_ENABLED": "false",
        "DLQ_PROCESSOR_ENABLED": "false",
        "ENABLE_RATE_LIMITING": "false",
        "RUN_HTTP_API": "true",
        "RUN_WEBHOOK_RECEIVER": "false",
        "RUN_STREAM_CONSUMER": "false",
        "RUN_LIFECYCLE_POLLER": "false",
    }
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()

    from src.api.app import create_app

    app = create_app()
    paths = _openapi_paths(app)
    assert "/health" in paths
    assert "/webhook/smartsheet" not in paths
    assert any(p.startswith("/configs") for p in paths)


def test_create_app_mounts_webhook_when_receiver_enabled(monkeypatch):
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test-key",
        "APP_CRYPTO_KEY": "0" * 64,
        "WEBHOOK_LIFECYCLE_ENABLED": "false",
        "DLQ_PROCESSOR_ENABLED": "false",
        "ENABLE_RATE_LIMITING": "false",
        "RUN_HTTP_API": "true",
        "RUN_WEBHOOK_RECEIVER": "true",
        "RUN_STREAM_CONSUMER": "false",
        "RUN_LIFECYCLE_POLLER": "false",
    }
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("APP_MODE", raising=False)
    get_settings.cache_clear()

    from src.api.app import create_app

    app = create_app()
    assert "/webhook/smartsheet" in _openapi_paths(app)
