"""CORS allowlist builder for I2C."""

from src.api.cors_config import build_i2c_allowed_origins
from src.config.settings import Settings


def test_build_i2c_allowed_origins_includes_hub_urls():
    s = Settings(
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="k",
        app_crypto_key="0123456789abcdef0123456789abcdef",
        environment="development",
    )
    origins = build_i2c_allowed_origins(s)
    assert "https://app.prodactivelabs.com" in origins
    assert "http://localhost:3000" in origins


def test_production_strict_drops_localhost():
    s = Settings(
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="k",
        app_crypto_key="0123456789abcdef0123456789abcdef",
        environment="production",
        i2c_cors_strict=True,
        cors_origin="https://custom.example.com",
    )
    origins = build_i2c_allowed_origins(s)
    assert "http://localhost:3000" not in origins
    assert "https://custom.example.com" in origins
