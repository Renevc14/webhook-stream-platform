"""Gateway/tool webhook contracts that are not tied to a single provider.

- ``secret_cache`` — Redis key naming for the shared-secret verification cache
- ``platform_registrations`` — ``platform_webhook_registrations`` SoT helpers
"""

from smarthub_core.webhooks.platform_registrations import (
    DEFAULT_PROVIDER as PLATFORM_DEFAULT_PROVIDER,
    PLATFORM_DELETE_BY_WEBHOOK_SQL,
    PLATFORM_DELETE_BY_WORKSHEET_SQL,
    PLATFORM_UPSERT_SQL,
    PLATFORM_WEBHOOKS_TABLE,
    PlatformRegistrationError,
    count_platform_vs_tool_parity,
    delete_platform_registration,
    delete_platform_registration_by_worksheet,
    upsert_platform_registration,
)
from smarthub_core.webhooks.secret_cache import (
    DEFAULT_PROVIDER,
    DEFAULT_SECRETS_TTL_S,
    SECRET_CACHE_KEY,
    secret_cache_key,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_SECRETS_TTL_S",
    "PLATFORM_DEFAULT_PROVIDER",
    "PLATFORM_DELETE_BY_WEBHOOK_SQL",
    "PLATFORM_DELETE_BY_WORKSHEET_SQL",
    "PLATFORM_UPSERT_SQL",
    "PLATFORM_WEBHOOKS_TABLE",
    "PlatformRegistrationError",
    "SECRET_CACHE_KEY",
    "count_platform_vs_tool_parity",
    "delete_platform_registration",
    "delete_platform_registration_by_worksheet",
    "secret_cache_key",
    "upsert_platform_registration",
]
