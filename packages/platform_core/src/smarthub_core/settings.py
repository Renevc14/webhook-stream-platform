"""
CoreSettings — configuration for smarthub_core.

Loaded from environment variables. Each backend can override defaults by
passing a CoreSettings instance into the relevant client constructor, or by
relying on the singleton returned by `get_core_settings()`.
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Crypto
    app_crypto_key: str = Field(
        ...,
        description="Base64-encoded 32-byte AES-GCM key. Shared across all Smarthub backends.",
    )

    # Redis
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        description="Redis connection URL. Production uses ElastiCache; local uses Docker container.",
    )
    redis_max_connections: int = Field(default=50, ge=1, le=500)
    redis_socket_timeout_s: float = Field(default=5.0, gt=0)

    # Supabase
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None
    supabase_max_connections: int = Field(default=5, ge=1, le=50)
    supabase_max_keepalive: int = Field(default=3, ge=0, le=50)
    supabase_keepalive_expiry_s: float = Field(default=20.0, gt=0)
    supabase_timeout_s: float = Field(default=30.0, gt=0)
    supabase_max_concurrent_ops: int = Field(default=4, ge=1, le=100)


@lru_cache(maxsize=1)
def get_core_settings() -> CoreSettings:
    return CoreSettings()
