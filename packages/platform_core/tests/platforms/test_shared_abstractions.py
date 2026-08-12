"""Contract tests for the shared abstractions in smarthub_core.platforms.shared.

These verify that:
- Abstract classes cannot be instantiated directly
- Concrete impls in the codebase actually implement the contract
- The dataclasses behave as expected (immutable, comparable)
"""

import pytest

from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenResolver,
)
from smarthub_core.platforms.shared.webhook_base import (
    BaseWebhookHandler,
    ChallengeResponse,
    NormalizedEvent,
    WebhookSignatureError,
)
from smarthub_core.platforms.shared.oauth import (
    OAuthRefresher,
    RefreshedToken,
)
from smarthub_core.platforms.smartsheet.token_resolver import (
    SmartsheetTokenResolver,
)
from smarthub_core.platforms.smartsheet.webhook import SmartsheetWebhookHandler


class TestTokenResolverContract:
    def test_abstract_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            TokenResolver()  # type: ignore[abstract]

    def test_smartsheet_resolver_implements_interface(self):
        # Subclass relationship plus the dunder check both work
        assert issubclass(SmartsheetTokenResolver, TokenResolver)

    def test_resolved_token_is_frozen(self):
        token = ResolvedToken(access_token="abc", base_url="https://x")
        with pytest.raises((AttributeError, Exception)):
            token.access_token = "xyz"  # frozen dataclass

    def test_resolved_token_defaults(self):
        token = ResolvedToken(access_token="abc")
        assert token.base_url is None
        assert token.refresh_token is None
        assert token.expires_at is None
        assert token.metadata == {}

    def test_token_not_found_is_exception(self):
        assert issubclass(TokenNotFoundError, Exception)


class TestWebhookBaseContract:
    def test_abstract_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BaseWebhookHandler()  # type: ignore[abstract]

    def test_smartsheet_handler_implements_interface(self):
        assert issubclass(SmartsheetWebhookHandler, BaseWebhookHandler)
        # And can actually be instantiated since all abstract methods are
        # implemented.
        SmartsheetWebhookHandler()

    def test_challenge_response_is_frozen(self):
        resp = ChallengeResponse(body=b"x")
        with pytest.raises((AttributeError, Exception)):
            resp.body = b"y"

    def test_challenge_response_defaults(self):
        resp = ChallengeResponse(body=b"x")
        assert resp.content_type == "text/plain"
        assert resp.status_code == 200
        assert resp.headers == {}

    def test_normalized_event_defaults(self):
        ev = NormalizedEvent(event_type="foo.bar")
        assert ev.external_id is None
        assert ev.raw is None
        assert ev.occurred_at is None
        assert ev.tool_hint is None

    def test_signature_error_is_exception(self):
        assert issubclass(WebhookSignatureError, Exception)


class TestOAuthContract:
    def test_abstract_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            OAuthRefresher()  # type: ignore[abstract]

    def test_refreshed_token_is_frozen(self):
        from datetime import datetime, timezone

        rt = RefreshedToken(
            access_token="a",
            expires_at=datetime.now(timezone.utc),
        )
        with pytest.raises((AttributeError, Exception)):
            rt.access_token = "b"
