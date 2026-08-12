from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    port: int = 3021
    product_slug: str = "smart-qr"
    change_agent_id: str = "smart-qr-v1"
    database_url: str = "postgresql://smartqr:smartqr@localhost:5433/smartqr"
    # auto = TLS for any non-local host. require | disable to force it.
    database_ssl: str = "auto"
    # None = 0 when connecting through a pgBouncer pooler, asyncpg default otherwise.
    database_statement_cache_size: Optional[int] = None
    redis_url: str = "redis://localhost:6380/0"
    run_stream_consumer: bool = True
    smartsheet_mode: str = "live"  # live | mock
    # Local-only fallback. Outside APP_ENV=local credentials come from the broker
    # below, because one shared token cannot represent several organizations.
    smartsheet_access_token: Optional[str] = None
    # encrypt-tool credentials service: resolves the per-user, per-tool OAuth
    # token and the org's regional API base. Mandatory outside local.
    encrypt_tool_internal_url: str = ""
    smartsheet_broker_secret: str = ""
    supabase_jwt_secret: str = ""
    supabase_url: str = ""
    # Service role key: only used to read Hub tables (memberships/profiles) whose
    # RLS policies are written for browser sessions. Never exposed to the browser.
    supabase_service_role_key: str = ""
    app_crypto_key: str = (
        # Valid base64(32-byte) local default — same wire format as platform APP_CRYPTO_KEY.
        # Staging/prod must inject the shared Secrets Manager value.
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    smartqr_billing_mode: str = "mock"  # mock (local only) | bolts
    gateway_internal_url: str = ""
    gateway_m2m_token: str = ""
    # Public base of the shared Webhook Gateway (ALB host, or the ngrok tunnel
    # pointing at a local gateway). Register builds
    # {base}/webhook/smartsheet/smart-qr/{org_id} from it. Required to register.
    smartsheet_webhook_callback_base_url: str = ""
    migrations_dir: str = Field(default="migrations")
    log_level: str = "INFO"
    # Comma-separated browser origins allowed to call the API. Empty = localhost dev only.
    cors_origin: str = ""

    @property
    def is_local(self) -> bool:
        """Only a developer machine. Deployed environments (dev/test/staging) are not local."""
        return self.app_env.strip().lower() == "local"

    @property
    def cors_allowed_origins(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origin.split(",") if o.strip()]
        if origins:
            return origins
        return ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
