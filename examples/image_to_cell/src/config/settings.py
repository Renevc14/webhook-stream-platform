"""
Centralized Settings Module
============================

All environment variables and configuration in one place.
Uses Pydantic Settings for validation and type safety.
"""

from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    All configuration is centralized here for easy management.
    Use get_settings() to access the singleton instance.
    """

    # ==========================================================================
    # Supabase Configuration
    # ==========================================================================
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_service_role_key: str = Field(..., description="Supabase service role key")

    # ==========================================================================
    # Encryption
    # ==========================================================================
    app_crypto_key: str = Field(..., description="AES-256 key for token encryption")

    # ==========================================================================
    # Product Identity
    # ==========================================================================
    product_slug: str = Field(
        default="smart-image-to-cell",
        description="Product identifier for Smartsheet tokens"
    )

    # ==========================================================================
    # Smartsheet OAuth token broker (encrypt-tool credentials service)
    # ==========================================================================
    encrypt_tool_internal_url: Optional[str] = Field(
        default=None,
        description="Base URL of the encrypt-tool token broker (no trailing slash)",
    )
    smartsheet_broker_secret: Optional[str] = Field(
        default=None,
        description="Shared secret for the broker's X-Broker-Secret header",
    )

    # ==========================================================================
    # Smartsheet Rate Limiting
    # ==========================================================================
    smartsheet_max_calls_per_minute: int = Field(
        default=275,
        ge=1,
        le=300,
        description="Max API calls per minute (Smartsheet limit is 300)"
    )
    smartsheet_rate_interval_s: float = Field(
        default=0.25,
        ge=0.1,
        description="Minimum seconds between API calls"
    )

    # ==========================================================================
    # Parallelism - Organization Level
    # ==========================================================================
    max_concurrent_orgs: int = Field(
        default=8,
        ge=1,
        le=20,
        description="Max organizations processed in parallel"
    )

    # ==========================================================================
    # Parallelism - Row Level
    # ==========================================================================
    max_concurrent_rows: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Max rows processed in parallel per organization"
    )

    # ==========================================================================
    # Parallelism - Image Level
    # ==========================================================================
    max_parallel_images: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Max images processed in parallel per row"
    )
    enable_parallel_image_processing: bool = Field(
        default=True,
        description="Enable parallel image processing within rows"
    )
    name_map_max_upload_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max upload attempts per attachment in name_map mode before giving up",
    )

    # ==========================================================================
    # Backfill Configuration
    # ==========================================================================
    backfill_max_pages: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max pages to fetch during backfill (1 page = 1000 rows)"
    )
    backfill_max_workers: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Max parallel workers for backfill processing"
    )

    # ==========================================================================
    # Polling Configuration
    # ==========================================================================
    polling_interval_s: int = Field(
        default=70,
        ge=10,
        description="Seconds between polling cycles"
    )

    # ==========================================================================
    # Webhook Configuration
    # ==========================================================================
    webhook_callback_url: Optional[str] = Field(
        default=None,
        description=(
            "Legacy shared callback URL for Smartsheet webhooks "
            "(e.g. https://smart-image-to-cell…/webhook/smartsheet). "
            "Ignored for register/lifecycle when "
            "SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL is set."
        ),
    )
    smartsheet_webhook_callback_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL",
            "smartsheet_webhook_callback_base_url",
        ),
        description=(
            "Shared Webhook Gateway origin (e.g. https://webhooks.prodactivelabs.com). "
            "When set, register + lifecycle enforce per-org URLs "
            "{base}/webhook/smartsheet/{product_slug}/{organization_id}."
        ),
    )
    gateway_internal_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "GATEWAY_INTERNAL_URL", "gateway_internal_url"
        ),
        description=(
            "Base URL for gateway m2m calls (secret push / ensure-registration). "
            "Use the public or internal ALB host that reaches the gateway API."
        ),
    )
    gateway_m2m_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GATEWAY_M2M_TOKEN", "gateway_m2m_token"),
        description="Shared Bearer token for gateway /internal/v1 m2m routes.",
    )
    webhook_lifecycle_enabled: bool = Field(
        default=True,
        description="Enable webhook lifecycle manager (auto-recovery, health checks)"
    )
    webhook_lifecycle_interval_s: int = Field(
        default=300,
        ge=60,
        description="Seconds between webhook health checks (default 5 minutes)"
    )
    max_concurrent_attachments: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max concurrent webhook attachment processing"
    )
    webhook_retry_max: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Max retry attempts for webhook operations"
    )
    webhook_retry_delay_base: float = Field(
        default=2.0,
        ge=1.0,
        description="Base delay for exponential backoff (2^attempt)"
    )
    webhook_processor_interval_s: int = Field(
        default=60,
        ge=10,
        description="Seconds between webhook processor cycles"
    )
    webhook_keepalive_interval_s: int = Field(
        default=3600,
        ge=300,
        description="Seconds between webhook keepalive pings (default 1 hour)"
    )
    webhook_processing_modes: str = Field(
        default="webhook",
        description="Comma-separated processing modes managed by v3. Production: webhook. Testing: webhook_v3 or webhook,webhook_v3."
    )

    # ==========================================================================
    # Dead Letter Queue / Retry Configuration
    # ==========================================================================
    dlq_processor_enabled: bool = Field(
        default=True,  # Requires imagetocell_failed_events table in Supabase
        description="Enable automatic retry of failed events from DLQ"
    )
    dlq_processor_interval_s: int = Field(
        default=300,
        ge=60,
        description="Seconds between DLQ processing cycles (default 5 minutes)"
    )
    dlq_batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of events to process per DLQ cycle"
    )
    dlq_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retry attempts before abandoning an event"
    )

    # ==========================================================================
    # Redis (SH-99 — cross-process webhook dedup)
    # ==========================================================================
    redis_url: Optional[str] = Field(
        default=None,
        description=(
            "Redis connection URL (e.g. redis://localhost:6380/0 or "
            "rediss://user:pass@host:6379/0). When unset, the service "
            "falls back to in-memory dedup with a startup WARN. Required "
            "in production for cross-task dedup."
        ),
    )
    redis_dedup_ttl_s: int = Field(
        default=600,
        ge=60,
        description=(
            "TTL for Redis dedup keys (dedup:i2c:processing:* and "
            "dedup:i2c:completed:*). Matches the legacy in-memory cache TTL."
        ),
    )
    backfill_lock_ttl_s: int = Field(
        default=600,
        ge=60,
        description=(
            "TTL for the distributed backfill lock "
            "(lock:backfill:worksheet:{worksheet_id}). Extended via heartbeat "
            "while the backfill runs (SH-148)."
        ),
    )
    backfill_lock_heartbeat_s: int = Field(
        default=60,
        ge=10,
        le=300,
        description=(
            "Interval between backfill lock heartbeat extensions. Should be "
            "well below backfill_lock_ttl_s (SH-148)."
        ),
    )

    # ==========================================================================
    # Image Processing
    # ==========================================================================
    image_target_max_px: int = Field(
        default=720,
        ge=100,
        le=2000,
        description="Max dimension (width or height) for processed images"
    )
    image_min_dimension_px: int = Field(
        default=50,
        description="Minimum dimension for valid images"
    )
    image_max_dimension_px: int = Field(
        default=5000,
        description="Maximum dimension for valid images"
    )
    jpeg_quality_levels: str = Field(
        default="75,65,50,40",
        description="Comma-separated JPEG quality levels to try"
    )
    enable_svg_support: bool = Field(
        default=False,
        description="Enable SVG to JPG conversion (beta)"
    )

    # ==========================================================================
    # HTTP Client
    # ==========================================================================
    http_pool_connections: int = Field(
        default=20,
        ge=5,
        description="Number of connection pools to cache"
    )
    http_pool_maxsize: int = Field(
        default=50,
        ge=10,
        description="Max connections per pool"
    )
    http_timeout_s: int = Field(
        default=30,
        ge=5,
        description="HTTP request timeout in seconds"
    )
    http_retry_total: int = Field(
        default=3,
        ge=1,
        description="Total retry attempts for HTTP requests"
    )
    http_retry_backoff_factor: float = Field(
        default=0.5,
        ge=0.1,
        description="Backoff factor for HTTP retries"
    )

    # ==========================================================================
    # Memory Management
    # ==========================================================================
    enable_memory_optimization: bool = Field(
        default=True,
        description="Enable memory usage monitoring and optimization"
    )
    max_memory_usage_percent: int = Field(
        default=85,
        ge=50,
        le=95,
        description="Max memory usage before triggering cleanup"
    )

    # ==========================================================================
    # Sheet Limits
    # ==========================================================================
    max_sheet_pages: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Max pages to fetch per sheet (1 page = 1000 rows)"
    )
    max_orgs_per_cycle: int = Field(
        default=200,
        ge=1,
        description="Max organizations to process per polling cycle"
    )
    max_worksheets_per_cycle: int = Field(
        default=50,
        ge=1,
        description="Max worksheets to process per polling cycle"
    )

    # ==========================================================================
    # Supabase Retry
    # ==========================================================================
    supabase_retry_max: int = Field(
        default=3,
        ge=1,
        description="Max retry attempts for Supabase operations"
    )
    supabase_retry_base_delay: float = Field(
        default=1.0,
        ge=0.5,
        description="Base delay for Supabase retry backoff"
    )

    # ==========================================================================
    # Cache Configuration
    # ==========================================================================
    cache_ttl_s: int = Field(
        default=900,
        ge=60,
        description="Cache TTL in seconds (default 15 min)"
    )
    cache_max_size: int = Field(
        default=1000,
        ge=100,
        description="Max cache entries before eviction"
    )

    # ==========================================================================
    # Logging
    # ==========================================================================
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )
    log_format: str = Field(
        default="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        description="Log format string"
    )

    # ==========================================================================
    # API Server
    # ==========================================================================
    host: str = Field(default="0.0.0.0", description="API server host")
    port: int = Field(default=3011, description="API server port")
    workers: int = Field(default=4, ge=1, description="Number of uvicorn workers")
    environment: str = Field(default="development", description="Environment (development, staging, production)")

    @property
    def is_local(self) -> bool:
        """Laptop / dockerless only. Staging and production stay fail-closed for billing."""
        return self.environment.strip().lower() in {"local", "development"}

    # ==========================================================================
    # Hub JWT (Attachment Counter / BAU parity)
    # ==========================================================================
    supabase_jwt_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_JWT_SECRET", "JWT_SECRET"),
        description="Supabase JWT Secret (Dashboard → Settings → API). Enables Bearer verification.",
    )
    i2c_jwt_optional_in_production: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "I2C_JWT_OPTIONAL_IN_PRODUCTION",
            "i2c_jwt_optional_in_production",
        ),
        description=(
            "If true: production without SUPABASE_JWT_SECRET uses legacy mode (no Bearer) instead of 503. "
            "Use only briefly while adding the key to Secrets Manager; default false."
        ),
    )

    # ==========================================================================
    # CORS (strict mode for production)
    # ==========================================================================
    cors_origin: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("CORS_ORIGIN", "cors_origin"),
        description="Optional comma-separated extra origins (env: CORS_ORIGIN)",
    )
    i2c_cors_strict: bool = Field(
        default=False,
        validation_alias=AliasChoices("I2C_CORS_STRICT", "i2c_cors_strict"),
        description="If true in production, CORS uses explicit hub origins only (env: I2C_CORS_STRICT)",
    )

    # ==========================================================================
    # OpenAPI / docs (env: I2C_DISABLE_OPENAPI=1)
    # ==========================================================================
    i2c_disable_openapi: bool = Field(
        default=False,
        validation_alias=AliasChoices("I2C_DISABLE_OPENAPI", "i2c_disable_openapi"),
        description="Disable /docs, /redoc, /openapi.json regardless of environment",
    )

    # ==========================================================================
    # API Security
    # ==========================================================================
    api_key: Optional[str] = Field(default=None, description="API key for internal services")
    i2c_admin_api_key: Optional[str] = Field(
        default=None,
        description="Admin API key for /dlq and /metrics (X-Admin-Key header). Set I2C_ADMIN_API_KEY in .env.",
    )
    enable_rate_limiting: bool = Field(default=True, description="Enable API rate limiting")
    api_rate_limit: int = Field(default=60, ge=1, description="Requests per minute limit")
    api_burst_size: int = Field(default=10, ge=1, description="Burst size for rate limiting")
    webhook_rate_limit: int = Field(
        default=100,
        ge=1,
        description="Webhook requests per minute per IP (DDoS protection)",
    )
    webhook_burst_size: int = Field(
        default=20,
        ge=1,
        description="Webhook burst size (Smartsheet can send bursts)",
    )

    # ==========================================================================
    # Logging Extended
    # ==========================================================================
    log_json_format: bool = Field(default=False, description="Use JSON format for logs")
    log_webhook_body: bool = Field(
        default=False,
        alias="I2C_LOG_WEBHOOK_BODY",
        description=(
            "When true, log the raw Smartsheet webhook body at INFO level. "
            "OFF by default — bodies are large (5-30KB) and contain "
            "customer-scoped IDs. Turn on only while investigating an "
            "incident."
        ),
    )

    # ==========================================================================
    # Polling Extended
    # ==========================================================================
    enable_polling: bool = Field(default=False, description="Enable background polling")

    # ==========================================================================
    # Runtime role flags (SH-151 — monolith default)
    # ==========================================================================
    run_http_api: bool = Field(
        default=True,
        validation_alias=AliasChoices("RUN_HTTP_API", "run_http_api"),
        description=(
            "Mount Hub/API routes (configs, DLQ, Smartsheet picker, backfill). "
            "Default true. Does not alone control the Smartsheet webhook receiver "
            "(see run_webhook_receiver)."
        ),
    )
    run_webhook_receiver: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "RUN_WEBHOOK_RECEIVER", "run_webhook_receiver"
        ),
        description=(
            "Mount the direct Smartsheet webhook route (/webhook/smartsheet). "
            "Default true. Set false after gateway cutover so configs/health stay "
            "up while Smartsheet callbacks hit the shared gateway (SH-211 / SH-206)."
        ),
    )
    run_stream_consumer: bool = Field(
        default=True,
        validation_alias=AliasChoices("RUN_STREAM_CONSUMER", "run_stream_consumer"),
        description="Start embedded stream:i2c consumer task. Default true.",
    )
    run_lifecycle_poller: bool = Field(
        default=True,
        validation_alias=AliasChoices("RUN_LIFECYCLE_POLLER", "run_lifecycle_poller"),
        description="Run webhook lifecycle manager in this process. Default true.",
    )
    app_mode: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("APP_MODE", "app_mode"),
        description=(
            "Optional legacy alias: all | api | worker-i2c. "
            "When set, overrides run_http_api / run_stream_consumer / run_lifecycle_poller."
        ),
    )

    @model_validator(mode="after")
    def _apply_app_mode(self) -> "Settings":
        if not self.app_mode:
            return self
        mode = self.app_mode.strip().lower()
        if mode == "all":
            object.__setattr__(self, "run_http_api", True)
            object.__setattr__(self, "run_stream_consumer", True)
            object.__setattr__(self, "run_lifecycle_poller", True)
        elif mode == "api":
            object.__setattr__(self, "run_http_api", True)
            object.__setattr__(self, "run_stream_consumer", False)
            object.__setattr__(self, "run_lifecycle_poller", True)
        elif mode in ("worker-i2c", "worker"):
            object.__setattr__(self, "run_http_api", False)
            object.__setattr__(self, "run_stream_consumer", True)
            object.__setattr__(self, "run_lifecycle_poller", False)
        else:
            raise ValueError(
                f"Invalid APP_MODE={self.app_mode!r}; expected all, api, or worker-i2c"
            )
        return self

    @property
    def runtime_roles_summary(self) -> str:
        return (
            f"http_api={self.run_http_api} "
            f"webhook_receiver={self.run_webhook_receiver} "
            f"stream_consumer={self.run_stream_consumer} "
            f"lifecycle_poller={self.run_lifecycle_poller}"
        )

    @property
    def tool_owns_webhook_lifecycle(self) -> bool:
        """True when this process still registers/heals Smartsheet webhooks.

        Pulse cutover sets ``WEBHOOK_LIFECYCLE_ENABLED=false`` and/or
        ``RUN_LIFECYCLE_POLLER=false`` so the shared gateway registrar owns
        create/heal; I2C then calls ensure-registration instead of Smartsheet.
        """
        return bool(self.webhook_lifecycle_enabled and self.run_lifecycle_poller)

    @property
    def webhook_processing_modes_list(self) -> list:
        """Parse processing modes into a list."""
        return [m.strip() for m in self.webhook_processing_modes.split(",") if m.strip()]

    def resolve_webhook_callback_url(self, organization_id: str) -> Optional[str]:
        """Callback URL to register / enforce for one org (gateway or legacy)."""
        from smarthub_core.platforms.smartsheet import resolve_webhook_callback_url

        return resolve_webhook_callback_url(
            organization_id=organization_id,
            gateway_base_url=self.smartsheet_webhook_callback_base_url,
            legacy_callback_url=self.webhook_callback_url,
            product_slug=self.product_slug,
        )

    def webhook_callback_ownership_patterns(self) -> list:
        """Substrings identifying webhooks owned by this deployment."""
        from smarthub_core.platforms.smartsheet import (
            webhook_callback_ownership_patterns,
        )

        return webhook_callback_ownership_patterns(
            gateway_base_url=self.smartsheet_webhook_callback_base_url,
            legacy_callback_url=self.webhook_callback_url,
            product_slug=self.product_slug,
        )

    @property
    def has_webhook_callback_config(self) -> bool:
        return bool(
            (self.smartsheet_webhook_callback_base_url or "").strip()
            or (self.webhook_callback_url or "").strip()
        )

    @property
    def jpeg_qualities(self) -> tuple:
        """Parse JPEG quality levels into tuple."""
        return tuple(int(q.strip()) for q in self.jpeg_quality_levels.split(","))

    @field_validator(
        "i2c_cors_strict",
        "i2c_disable_openapi",
        "i2c_jwt_optional_in_production",
        mode="before",
    )
    @classmethod
    def _parse_boolish(cls, v):
        if v is None or v == "":
            return False
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """
    Get singleton Settings instance.

    Uses LRU cache to ensure only one instance is created.

    Returns:
        Settings: Application settings
    """
    settings = Settings()

    # The Smartsheet token broker clients (smarthub_core token_resolver and
    # services/smartsheet/smartsheet_broker.py) read these via os.getenv;
    # pydantic-settings loads .env into the model only, so mirror them into
    # the process environment here.
    import os

    if settings.encrypt_tool_internal_url and not os.getenv("ENCRYPT_TOOL_INTERNAL_URL"):
        os.environ["ENCRYPT_TOOL_INTERNAL_URL"] = settings.encrypt_tool_internal_url
    if settings.smartsheet_broker_secret and not os.getenv("SMARTSHEET_BROKER_SECRET"):
        os.environ["SMARTSHEET_BROKER_SECRET"] = settings.smartsheet_broker_secret

    return settings
