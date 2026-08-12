"""Gateway settings."""

from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

from gateway.tools.registry import ToolRegistry, load_tool_registry

class Settings(BaseSettings):
    environment: str = "development"
    log_level: str = "INFO"
    log_json_format: bool = False
    gateway_port: int = 3020
    redis_url: Optional[str] = None

    # SH-173 — secret sync + lifecycle singleton
    run_lifecycle_poller: bool = True
    webhook_lifecycle_interval_s: int = 300
    lifecycle_lock_ttl_s: int = 600
    lifecycle_lock_heartbeat_s: int = 60
    webhook_secrets_ttl_s: int = 300

    # SH-228 — optional JSON array overlay for ToolRegistryEntry objects.
    # Built-ins (I2C + SmartQR) always load; this adds or replaces by tool_slug
    # without a gateway code change.
    gateway_tool_registry_json: Optional[str] = None

    # Deprecated per-tool knobs (SH-228): still honored to remap builtin slugs /
    # disable SmartQR secret sync. Prefer GATEWAY_TOOL_REGISTRY_JSON for new tools.
    i2c_tool_slug: str = "smart-image-to-cell"
    smartqr_tool_slug: str = "smart-qr"
    smartqr_secret_sync_enabled: bool = True

    # Supabase (sandbox / staging) — required when the lifecycle poller runs.
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None
    # Same APP_CRYPTO_KEY as all backends — decrypts webhook shared secrets.
    app_crypto_key: Optional[str] = None
    # Base URL for Smartsheet register (SH-173 / SH-175 / SH-210).
    # Example: https://webhooks.prodactivelabs.com
    smartsheet_webhook_callback_base_url: Optional[str] = None

    # SH-210 — gateway-owned register/renew/zombie. Default off: a tool must
    # stop its own lifecycle poller before it appears in WEBHOOK_REGISTRAR_TOOLS,
    # otherwise both sides fight over the same Smartsheet webhooks.
    run_webhook_registrar: bool = False
    webhook_registrar_tools: str = "smart-image-to-cell"
    webhook_registrar_min_age_minutes: float = 10.0
    # SH-230 D — platform_webhook_registrations is write SoT. When true, also
    # soft-fail mirror into per-tool *_webhooks for rollback until table drop.
    platform_webhook_dual_write: bool = True
    # SH-230 Phase C — secret sync reads platform_webhook_registrations instead of
    # per-tool *_webhooks. Requires migration + backfill + parity soak first.
    # Default false; staging cutover enables after backfill.
    platform_webhook_read: bool = False
    # encrypt-tool OAuth broker — required for OAuth-only orgs when the
    # registrar resolves Smartsheet tokens via SmartsheetClient.
    encrypt_tool_internal_url: Optional[str] = None
    smartsheet_broker_secret: Optional[str] = None
    # m2m for PUT /internal/v1/webhook-secrets and ensure-registration
    m2m_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GATEWAY_M2M_TOKEN", "M2M_TOKEN", "m2m_token"),
    )
    app_env: str = "development"

    @property
    def webhook_registrar_tools_list(self) -> list[str]:
        return [s.strip() for s in (self.webhook_registrar_tools or "").split(",") if s.strip()]

    def tool_registry(self) -> ToolRegistry:
        """Resolve the runtime tool registry (builtins + JSON overlay)."""
        return load_tool_registry(
            i2c_tool_slug=self.i2c_tool_slug,
            smartqr_tool_slug=self.smartqr_tool_slug,
            smartqr_secret_sync_enabled=self.smartqr_secret_sync_enabled,
            registry_json=self.gateway_tool_registry_json,
        )

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
