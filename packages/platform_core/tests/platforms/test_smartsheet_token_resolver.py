"""Unit tests for SmartsheetTokenResolver.

The resolver is the source of truth for Smartsheet credential resolution.
Tests cover:

- cache miss → Postgres read → cache write → decrypted return
- cache hit → no Postgres read
- corrupt cache → DEL + re-fetch
- force_refresh bypasses cache
- TokenNotFoundError when no row exists
- TokenNotFoundError when no base_url row exists
- invalidate() drops the cache key
- broker contract (SH-182): oauth_state metadata propagation, typed
  TokenRevokedError on 409 with no silent legacy fallback, plaintext never
  written to Redis
- broker unavailable with no PAT to fall back to → TokenTemporarilyUnavailableError,
  kept distinct from "nothing is connected"
"""

import base64
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from smarthub_core.crypto import CryptoService
from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.platforms.shared.token_resolver import (
    ResolvedToken,
    TokenNotFoundError,
    TokenRevokedError,
    TokenTemporarilyUnavailableError,
)
from smarthub_core.platforms.smartsheet.token_resolver import (
    SmartsheetTokenResolver,
)
from smarthub_core.settings import CoreSettings


TEST_KEY_B64 = base64.b64encode(b"\x42" * 32).decode()
TEST_PLAINTEXT_TOKEN = "ss-token-resolver-plaintext-1234567890"
TEST_BASE_URL = "https://api.smartsheet.com"
TEST_ORG = "00000000-0000-0000-0000-000000000099"
TEST_SLUG = "smart-image-to-cell"


def _settings() -> CoreSettings:
    return CoreSettings(
        app_crypto_key=TEST_KEY_B64,
        redis_url="redis://127.0.0.1:6379/15",
    )


@pytest.fixture
def crypto():
    return CryptoService(key=TEST_KEY_B64)


@pytest.fixture(autouse=True)
def _patch_decrypt(crypto, monkeypatch):
    from smarthub_core.platforms.smartsheet import token_resolver as mod

    def fake_decrypt(ciphertext: str) -> str:
        return crypto.decrypt(ciphertext)

    monkeypatch.setattr(mod, "decrypt_token", fake_decrypt)


@pytest.fixture
def encrypted_token(crypto) -> str:
    return crypto.encrypt(TEST_PLAINTEXT_TOKEN)


@pytest.fixture
def supabase_mock(encrypted_token):
    svc = MagicMock(spec=SupabaseService)

    token_response = MagicMock()
    token_response.data = [{"encrypted_token": encrypted_token}]

    base_response = MagicMock()
    base_response.data = [{"base_url": TEST_BASE_URL}]

    async def fake_execute(operation, *, description="", **kwargs):
        if "smartsheet_tokens" in description:
            return token_response
        if "organization_ss_api_base" in description:
            return base_response
        raise AssertionError(f"unexpected supabase call: {description}")

    svc.execute_with_retry_async = fake_execute
    return svc


@pytest.fixture
async def resolver(rc: RedisClient, supabase_mock):
    r = SmartsheetTokenResolver(
        redis=rc,
        supabase=supabase_mock,
        settings=_settings(),
    )
    # Clean any stale cache from a previous test run
    await rc.cache_del(SmartsheetTokenResolver.cache_key(TEST_ORG, TEST_SLUG))
    return r


class TestResolve:
    async def test_provider_field(self, resolver):
        assert resolver.provider == "smartsheet"

    async def test_cache_miss_reads_postgres_and_caches_ciphertext(
        self, resolver, rc, encrypted_token
    ):
        token = await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert isinstance(token, ResolvedToken)
        assert token.access_token == TEST_PLAINTEXT_TOKEN
        assert token.base_url == TEST_BASE_URL

        # Cache should contain ciphertext, never plaintext
        cached = await rc.cache_get(
            SmartsheetTokenResolver.cache_key(TEST_ORG, TEST_SLUG)
        )
        assert cached is not None
        parsed = json.loads(cached)
        assert parsed["ciphertext"] == encrypted_token
        assert parsed["base_url"] == TEST_BASE_URL
        assert TEST_PLAINTEXT_TOKEN not in cached

    async def test_cache_hit_skips_postgres(self, resolver, rc, supabase_mock):
        # Warm cache
        await resolver.resolve(TEST_ORG, TEST_SLUG)

        call_count = {"n": 0}
        original_execute = supabase_mock.execute_with_retry_async

        async def counting_execute(operation, *, description="", **kwargs):
            call_count["n"] += 1
            return await original_execute(
                operation, description=description, **kwargs
            )

        supabase_mock.execute_with_retry_async = counting_execute

        # Second resolve should hit cache, not Postgres
        token = await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert token.access_token == TEST_PLAINTEXT_TOKEN
        assert call_count["n"] == 0

    async def test_force_refresh_bypasses_cache(self, resolver, rc, supabase_mock):
        await resolver.resolve(TEST_ORG, TEST_SLUG)

        call_count = {"n": 0}
        original_execute = supabase_mock.execute_with_retry_async

        async def counting_execute(operation, *, description="", **kwargs):
            call_count["n"] += 1
            return await original_execute(
                operation, description=description, **kwargs
            )

        supabase_mock.execute_with_retry_async = counting_execute

        await resolver.resolve(TEST_ORG, TEST_SLUG, force_refresh=True)
        # 2 reads: smartsheet_tokens + organization_ss_api_base
        assert call_count["n"] == 2

    async def test_corrupt_cache_is_dropped_and_refetched(self, resolver, rc):
        key = SmartsheetTokenResolver.cache_key(TEST_ORG, TEST_SLUG)
        await rc.cache_set(key, "not-json-at-all", ttl_s=60)

        token = await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert token.access_token == TEST_PLAINTEXT_TOKEN

        # Cache is repopulated with valid JSON
        cached = await rc.cache_get(key)
        json.loads(cached)  # raises if still corrupt

    async def test_token_not_found_when_no_row(self, rc):
        svc = MagicMock(spec=SupabaseService)

        empty = MagicMock()
        empty.data = []

        async def fake_execute(operation, *, description="", **kwargs):
            return empty

        svc.execute_with_retry_async = fake_execute

        resolver = SmartsheetTokenResolver(
            redis=rc, supabase=svc, settings=_settings()
        )
        with pytest.raises(TokenNotFoundError):
            await resolver.resolve("missing-org", "missing-slug")

    async def test_token_not_found_when_no_base_url(self, rc, encrypted_token):
        svc = MagicMock(spec=SupabaseService)

        token_response = MagicMock()
        token_response.data = [{"encrypted_token": encrypted_token}]

        empty_base = MagicMock()
        empty_base.data = []

        async def fake_execute(operation, *, description="", **kwargs):
            if "smartsheet_tokens" in description:
                return token_response
            if "organization_ss_api_base" in description:
                return empty_base
            raise AssertionError("unexpected")

        svc.execute_with_retry_async = fake_execute

        resolver = SmartsheetTokenResolver(
            redis=rc, supabase=svc, settings=_settings()
        )
        with pytest.raises(TokenNotFoundError, match="base_url"):
            await resolver.resolve(TEST_ORG, TEST_SLUG)

    async def test_invalidate_drops_cache(self, resolver, rc):
        key = SmartsheetTokenResolver.cache_key(TEST_ORG, TEST_SLUG)
        await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert await rc.cache_get(key) is not None

        await resolver.invalidate(TEST_ORG, TEST_SLUG)
        assert await rc.cache_get(key) is None

    async def test_empty_args_raise(self, resolver):
        with pytest.raises(TokenNotFoundError):
            await resolver.resolve("", TEST_SLUG)
        with pytest.raises(TokenNotFoundError):
            await resolver.resolve(TEST_ORG, "")


# ---------------------------------------------------------------------------
# Broker contract (SH-182)
# ---------------------------------------------------------------------------
# When ENCRYPT_TOOL_INTERNAL_URL + SMARTSHEET_BROKER_SECRET are set, the
# resolver goes broker-first. These tests exercise the OAuth-state contract:
# metadata propagation on 200, typed TokenRevokedError on 409 (with no silent
# fallback into the legacy PAT tables), and no plaintext in Redis.

BROKER_TOKEN = "broker-plaintext-token-abc"
BROKER_BASE = "https://api.smartsheet.com/2.0"


class _FakeHttpResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; counts posts, returns a canned response."""

    calls = 0
    response: _FakeHttpResponse = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls += 1
        return type(self).response


@pytest.fixture
def broker_env(monkeypatch):
    from smarthub_core.platforms.smartsheet import token_resolver as mod

    monkeypatch.setenv("ENCRYPT_TOOL_INTERNAL_URL", "http://broker.test:3051")
    monkeypatch.setenv("SMARTSHEET_BROKER_SECRET", "test-broker-secret")
    mod._broker_cache.clear()
    _FakeAsyncClient.calls = 0
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)
    yield mod
    mod._broker_cache.clear()


@pytest.fixture
def redis_mock():
    rc = MagicMock(spec=RedisClient)
    rc.cache_get = AsyncMock(return_value=None)
    rc.cache_set = AsyncMock()
    rc.cache_del = AsyncMock()
    return rc


@pytest.fixture
def supabase_never_called():
    """Supabase mock that fails the test if the legacy path is ever taken."""
    svc = MagicMock(spec=SupabaseService)

    async def fail(*args, **kwargs):
        raise AssertionError("legacy Postgres path must not be used")

    svc.execute_with_retry_async = fail
    return svc


class TestBrokerContract:
    async def test_broker_200_propagates_metadata(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
            "source": "pat",
            "oauth_state": "revoked",
            "fallback_used": True,
            "fallback_reason": "CONNECTION_REVOKED",
            "smartsheet_user_email": "runner@example.com",
            "region": "US",
            "request_id": "rid-1",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        token = await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert token.access_token == BROKER_TOKEN
        assert token.base_url == BROKER_BASE
        assert token.metadata["source"] == "pat"
        assert token.metadata["oauth_state"] == "revoked"
        assert token.metadata["fallback_used"] is True
        assert token.metadata["fallback_reason"] == "CONNECTION_REVOKED"
        assert token.metadata["smartsheet_user_email"] == "runner@example.com"
        # request_id is not identity metadata — never propagated.
        assert "request_id" not in token.metadata

    async def test_broker_healthy_oauth_metadata(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
            "source": "oauth",
            "oauth_state": "active",
            "fallback_used": False,
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        token = await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert token.metadata["oauth_state"] == "active"
        assert token.metadata["fallback_used"] is False
        assert "fallback_reason" not in token.metadata

    async def test_broker_200_compatible_with_token_only_callers(
        self, broker_env, redis_mock, supabase_never_called
    ):
        """Pre-SH-182 responses (no state fields) still resolve cleanly."""
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        token = await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert token.access_token == BROKER_TOKEN
        assert token.base_url == BROKER_BASE

    async def test_broker_409_raises_typed_error_without_legacy_fallback(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(409, {
            "error_code": "CONNECTION_REVOKED",
            "oauth_state": "revoked",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        with pytest.raises(TokenRevokedError) as exc_info:
            await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert exc_info.value.oauth_state == "revoked"
        assert exc_info.value.reason == "CONNECTION_REVOKED"
        # Compatibility: existing callers that catch TokenNotFoundError keep
        # their needs-reconnect handling.
        assert isinstance(exc_info.value, TokenNotFoundError)
        # No plaintext anywhere near the error or Redis.
        assert BROKER_TOKEN not in str(exc_info.value)
        redis_mock.cache_set.assert_not_awaited()

    async def test_broker_409_terminal_reason(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(409, {
            "error_code": "TOKEN_REFRESH_FAILED",
            "oauth_state": "error",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        with pytest.raises(TokenRevokedError) as exc_info:
            await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert exc_info.value.oauth_state == "error"
        assert exc_info.value.reason == "TOKEN_REFRESH_FAILED"

    async def test_broker_404_raises_token_not_found(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(404, {
            "error_code": "NO_CREDENTIALS",
            "oauth_state": "missing",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        with pytest.raises(TokenNotFoundError):
            await resolver.resolve(TEST_ORG, TEST_SLUG)

    async def test_broker_5xx_falls_back_to_legacy_tables(
        self, broker_env, redis_mock, supabase_mock, encrypted_token
    ):
        """Transient broker failure (e.g. 503) is NOT authoritative — the
        resolver continues into the legacy PAT tables as before."""
        _FakeAsyncClient.response = _FakeHttpResponse(503, {
            "error_code": "OAUTH_TEMPORARILY_UNAVAILABLE",
            "oauth_state": "unknown",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_mock, settings=_settings()
        )
        token = await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert token.access_token == TEST_PLAINTEXT_TOKEN
        assert token.base_url == TEST_BASE_URL

    async def test_broker_plaintext_never_written_to_redis(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
            "source": "oauth",
            "oauth_state": "active",
            "fallback_used": False,
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        redis_mock.cache_set.assert_not_awaited()

    async def test_broker_cache_preserves_metadata(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
            "source": "pat",
            "oauth_state": "missing",
            "fallback_used": True,
            "fallback_reason": "NO_OAUTH_TOKEN",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        first = await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        second = await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert _FakeAsyncClient.calls == 1  # second hit served from cache
        assert second.access_token == first.access_token
        assert second.metadata["oauth_state"] == "missing"
        assert second.metadata["fallback_reason"] == "NO_OAUTH_TOKEN"

    async def test_invalidate_drops_broker_cache(
        self, broker_env, redis_mock, supabase_never_called
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(200, {
            "access_token": BROKER_TOKEN,
            "base_url": BROKER_BASE,
            "source": "oauth",
            "oauth_state": "active",
            "fallback_used": False,
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_never_called, settings=_settings()
        )
        await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        await resolver.invalidate(TEST_ORG, TEST_SLUG)
        await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")
        assert _FakeAsyncClient.calls == 2


# ---------------------------------------------------------------------------
# Broker unavailable with no PAT to fall back to
# ---------------------------------------------------------------------------
# A configured broker that cannot answer is a third outcome, distinct from both
# "connected" and "not connected". Reporting it as "not connected" tells a
# correctly-connected user to reconnect, and lets callers treat retryable work
# as permanently failed. Tools with no PAT rows at all (OAuth-only, e.g.
# smart-qr) hit this path on every broker blip, so it has to be explicit.


class _RaisingAsyncClient(_FakeAsyncClient):
    """Simulates a transport failure reaching the broker."""

    error: BaseException = ConnectionError("connection refused")

    async def post(self, url, json=None, headers=None):
        type(self).calls += 1
        raise type(self).error


@pytest.fixture
def supabase_empty():
    """No PAT rows — the shape of an OAuth-only tool."""
    svc = MagicMock(spec=SupabaseService)
    empty = MagicMock()
    empty.data = []

    async def fake_execute(operation, *, description="", **kwargs):
        return empty

    svc.execute_with_retry_async = fake_execute
    return svc


class TestBrokerUnavailable:
    async def test_transport_failure_without_pat_is_temporary(
        self, broker_env, monkeypatch, redis_mock, supabase_empty
    ):
        _RaisingAsyncClient.calls = 0
        _RaisingAsyncClient.error = ConnectionError("connection refused")
        monkeypatch.setattr(broker_env.httpx, "AsyncClient", _RaisingAsyncClient)
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenTemporarilyUnavailableError):
            await resolver.resolve(TEST_ORG, TEST_SLUG, user_id="user-1")

    async def test_5xx_without_pat_is_temporary(
        self, broker_env, redis_mock, supabase_empty
    ):
        _FakeAsyncClient.response = _FakeHttpResponse(503, {
            "error_code": "OAUTH_TEMPORARILY_UNAVAILABLE",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenTemporarilyUnavailableError):
            await resolver.resolve(TEST_ORG, TEST_SLUG)

    async def test_200_without_credentials_is_temporary(
        self, broker_env, redis_mock, supabase_empty
    ):
        """A 200 that breaks the contract is not evidence the user is disconnected."""
        _FakeAsyncClient.response = _FakeHttpResponse(200, {"source": "oauth"})
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenTemporarilyUnavailableError):
            await resolver.resolve(TEST_ORG, TEST_SLUG)

    async def test_temporary_is_still_a_token_not_found_error(
        self, broker_env, redis_mock, supabase_empty
    ):
        """Callers that only catch TokenNotFoundError keep working."""
        _FakeAsyncClient.response = _FakeHttpResponse(503, {})
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenNotFoundError):
            await resolver.resolve(TEST_ORG, TEST_SLUG)

    async def test_404_stays_not_connected(
        self, broker_env, redis_mock, supabase_empty
    ):
        """An authoritative "nothing connected" must not be softened into a blip."""
        _FakeAsyncClient.response = _FakeHttpResponse(404, {
            "error_code": "NO_CREDENTIALS",
        })
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenNotFoundError) as exc_info:
            await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert not isinstance(exc_info.value, TokenTemporarilyUnavailableError)

    async def test_missing_token_without_broker_stays_not_connected(
        self, monkeypatch, redis_mock, supabase_empty
    ):
        """Without a broker there is nothing transient about an empty table."""
        monkeypatch.delenv("ENCRYPT_TOOL_INTERNAL_URL", raising=False)
        monkeypatch.delenv("SMARTSHEET_BROKER_SECRET", raising=False)
        resolver = SmartsheetTokenResolver(
            redis=redis_mock, supabase=supabase_empty, settings=_settings()
        )

        with pytest.raises(TokenNotFoundError) as exc_info:
            await resolver.resolve(TEST_ORG, TEST_SLUG)
        assert not isinstance(exc_info.value, TokenTemporarilyUnavailableError)
