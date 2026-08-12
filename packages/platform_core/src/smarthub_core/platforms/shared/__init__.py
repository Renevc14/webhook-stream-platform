"""Provider-agnostic abstractions for the platforms layer.

- ``token_resolver`` — ``TokenResolver`` interface; one impl per provider
- ``webhook_base``  — ``BaseWebhookHandler`` for Webhook Gateway handlers
- ``oauth``         — refresh-token helper for providers that use OAuth
"""

from smarthub_core.platforms.shared.oauth import (
    OAuthRefreshError,
    OAuthRefresher,
    RefreshedToken,
)
from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenResolver,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)
from smarthub_core.platforms.shared.webhook_base import (
    BaseWebhookHandler,
    ChallengeResponse,
    NormalizedEvent,
    WebhookSignatureError,
)

__all__ = [
    "BaseWebhookHandler",
    "ChallengeResponse",
    "NormalizedEvent",
    "OAuthRefreshError",
    "OAuthRefresher",
    "RefreshedToken",
    "ResolvedToken",
    "TokenNotFoundError",
    "TokenResolver",
    "TokenRevokedError",
    "TokenTemporarilyUnavailableError",
    "WebhookSignatureError",
]
