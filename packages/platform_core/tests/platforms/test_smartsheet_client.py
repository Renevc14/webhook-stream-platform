"""
Unit tests for smarthub_core.platforms.smartsheet.client.

Both Redis (real, against the local container) and Supabase (mocked) are used:
- Redis behavior matters for the cache-aside flow and the rate limit, so we
  use the real `rc` fixture from conftest.
- Supabase calls are mocked because there's no local Supabase and these tests
  are about the SmartsheetClient layer, not the Supabase wrapper.
"""

import asyncio
import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from smarthub_core.crypto import CryptoService
from smarthub_core.db.redis import RedisClient
from smarthub_core.db.supabase import SupabaseService
from smarthub_core.platforms.smartsheet.client import (
    RETRYABLE_PATTERNS,
    SmartsheetClient,
    SmartsheetError,
    is_retryable_smartsheet_error,
)
from smarthub_core.settings import CoreSettings


def _test_settings(crypto_key_b64: str) -> CoreSettings:
    return CoreSettings(
        app_crypto_key=crypto_key_b64,
        redis_url="redis://127.0.0.1:6379/15",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TEST_KEY_B64 = base64.b64encode(b"\x42" * 32).decode()
TEST_PLAINTEXT_TOKEN = "ss-token-plaintext-abcdef-1234567890"
TEST_BASE_URL = "https://api.smartsheet.com"
TEST_ORG = "00000000-0000-0000-0000-000000000001"
TEST_SLUG = "smart-image-to-cell"


@pytest.fixture
def crypto():
    return CryptoService(key=TEST_KEY_B64)


@pytest.fixture(autouse=True)
def _patch_module_crypto(crypto, monkeypatch):
    """Make `decrypt_token` inside the resolver module use our test key.

    Decryption now lives in ``platforms.smartsheet.token_resolver``; the client
    delegates resolution to it. Patching that module's namespace ensures the
    test key is the one used for cache-aside decrypts.
    """
    from smarthub_core.platforms.smartsheet import token_resolver as resolver_module

    def fake_decrypt(ciphertext: str) -> str:
        return crypto.decrypt(ciphertext)

    monkeypatch.setattr(resolver_module, "decrypt_token", fake_decrypt)


@pytest.fixture
def encrypted_token(crypto) -> str:
    return crypto.encrypt(TEST_PLAINTEXT_TOKEN)


@pytest.fixture
def supabase_mock(encrypted_token):
    """Mock SupabaseService that returns a token + base URL on the right calls."""
    svc = MagicMock(spec=SupabaseService)

    token_response = MagicMock()
    token_response.data = [{"encrypted_token": encrypted_token}]

    base_response = MagicMock()
    base_response.data = [{"base_url": TEST_BASE_URL}]

    # supabase.execute_with_retry_async receives a sync callable that calls
    # supabase.table(...). We don't actually invoke the callable; we route
    # by the `description` argument to return the right response.
    async def fake_execute(operation, *, description="", **kwargs):
        if "smartsheet_tokens" in description:
            return token_response
        if "organization_ss_api_base" in description:
            return base_response
        raise AssertionError(f"unexpected supabase call: {description}")

    svc.execute_with_retry_async = fake_execute
    return svc


@pytest.fixture
async def client(rc: RedisClient, supabase_mock):
    return SmartsheetClient(
        org_id=TEST_ORG,
        product_slug=TEST_SLUG,
        redis=rc,
        supabase=supabase_mock,
        settings=_test_settings(TEST_KEY_B64),
        # Tighten so tests don't wait long for rate limit decisions
        rate_limit_window_s=2,
        rate_limit_max_calls=100,
    )


# ---------------------------------------------------------------------------
# is_retryable_smartsheet_error
# ---------------------------------------------------------------------------


class TestRetryablePatternMatching:
    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 500 Internal Server Error",
            "HTTP 502 Bad Gateway",
            "HTTP 503 Service Unavailable",
            'Smartsheet response: {"errorCode": 4000, "message": "An unexpected error occurred."}',
            # Hotfix 2026-06-17 (Beth incident): real prod errors from the
            # /cellimages 4004 wave. Both with and without space after the
            # colon since SDK formatters disagree.
            'Smartsheet response: {"errorCode": 4004, "message": "Unexpected error.", "shouldRetry": true}',
            'Smartsheet response: {"errorCode":4004,"shouldRetry":true,"statusCode":500}',
            # The generic shouldRetry flag — defense in depth against
            # future Smartsheet codes we haven't enumerated yet.
            'Smartsheet response: {"errorCode": 9999, "shouldRetry": true}',
            "<ConnectionTerminated error_code:1, last_stream_id:937>",
            "Server disconnected without sending a response",
            "Read timed out",
            "RemoteProtocolError",
        ],
    )
    def test_known_transient_messages_match(self, msg):
        assert is_retryable_smartsheet_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        "msg",
        [
            # Non-retryable Smartsheet errors that LOOK like they should
            # retry — explicit "shouldRetry": false must override our match.
            # NB: today our matcher doesn't actually inspect shouldRetry=false
            # explicitly; the existing 401/403/404 filter catches these by
            # other substrings. These assertions just ensure we don't
            # regress those cases when adding the "shouldretry": true
            # matchers.
            '403 Forbidden: {"errorCode": 1004, "shouldRetry": false}',
        ],
    )
    def test_should_retry_false_still_blocked_by_other_filters(self, msg):
        # 403 substring → not in retryable list → False
        assert is_retryable_smartsheet_error(Exception(msg)) is False

    @pytest.mark.parametrize(
        "msg",
        [
            "401 Unauthorized: invalid token",
            "403 Forbidden: insufficient permissions",
            "404 Not Found: sheet does not exist",
            "Invalid request: column ID 12345 does not exist on this sheet",
        ],
    )
    def test_non_transient_messages_do_not_match(self, msg):
        assert is_retryable_smartsheet_error(Exception(msg)) is False


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    async def test_requires_org_and_slug(self, rc: RedisClient, supabase_mock):
        s = _test_settings(TEST_KEY_B64)
        with pytest.raises(SmartsheetError, match="org_id and product_slug"):
            SmartsheetClient(
                org_id="",
                product_slug=TEST_SLUG,
                redis=rc,
                supabase=supabase_mock,
                settings=s,
            )
        with pytest.raises(SmartsheetError, match="org_id and product_slug"):
            SmartsheetClient(
                org_id=TEST_ORG,
                product_slug="",
                redis=rc,
                supabase=supabase_mock,
                settings=s,
            )

    async def test_keys_use_correct_naming(self, client: SmartsheetClient):
        assert client._token_key == f"ss-token:{TEST_ORG}:{TEST_SLUG}"  # type: ignore[attr-defined]
        assert client._rate_limit_key == f"ratelimit:ss-api:{TEST_ORG}:{TEST_SLUG}"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Credential resolution (cache-aside + Postgres fallback)
# ---------------------------------------------------------------------------


class TestCredentialResolution:
    async def test_first_call_reads_postgres_and_caches_ciphertext(
        self, client: SmartsheetClient, rc: RedisClient
    ):
        token, base_url = await client._resolve_credentials()
        assert token == TEST_PLAINTEXT_TOKEN
        assert base_url == TEST_BASE_URL

        # Cache must contain CIPHERTEXT, not plaintext
        cached = await rc.cache_get(client._token_key)
        assert cached is not None
        parsed = json.loads(cached)
        assert "ciphertext" in parsed
        assert parsed["base_url"] == TEST_BASE_URL
        # Plaintext token must NEVER appear in Redis
        assert TEST_PLAINTEXT_TOKEN not in cached

    async def test_second_call_uses_cache(
        self, client: SmartsheetClient, supabase_mock
    ):
        # Replace mock with a counting version so we can assert call counts
        original_execute = supabase_mock.execute_with_retry_async
        call_count = {"n": 0}

        async def counting_execute(*args, **kwargs):
            call_count["n"] += 1
            return await original_execute(*args, **kwargs)

        supabase_mock.execute_with_retry_async = counting_execute

        # 1st call: 2 Postgres queries (token + base_url)
        await client._resolve_credentials()
        assert call_count["n"] == 2

        # 2nd call: 0 Postgres queries (cache hit)
        await client._resolve_credentials()
        assert call_count["n"] == 2

    async def test_force_refresh_skips_cache(self, client: SmartsheetClient):
        # Prime the cache
        await client._resolve_credentials()
        # Corrupt the cache to ensure force_refresh is going to Postgres
        await client._redis.cache_set(
            client._token_key, json.dumps({"ciphertext": "garbage", "base_url": "x"}), ttl_s=60
        )
        token, base_url = await client._resolve_credentials(force_refresh=True)
        assert token == TEST_PLAINTEXT_TOKEN
        assert base_url == TEST_BASE_URL

    async def test_corrupt_cache_falls_back_to_postgres(
        self, client: SmartsheetClient, rc: RedisClient
    ):
        # Plant non-JSON in the cache
        await rc.cache_set(client._token_key, "not-json-at-all", ttl_s=60)
        token, base_url = await client._resolve_credentials()
        assert token == TEST_PLAINTEXT_TOKEN
        # Should have re-cached with valid JSON
        cached_now = await rc.cache_get(client._token_key)
        parsed = json.loads(cached_now)
        assert "ciphertext" in parsed

    async def test_invalidate_clears_cache_and_sdk_client(
        self, client: SmartsheetClient
    ):
        await client._resolve_credentials()
        assert await client._redis.cache_get(client._token_key) is not None
        await client.invalidate_credentials_cache()
        assert await client._redis.cache_get(client._token_key) is None


# ---------------------------------------------------------------------------
# call_with_retry_async
# ---------------------------------------------------------------------------


class TestCallWithRetry:
    async def test_succeeds_first_try(self, client: SmartsheetClient):
        called = []

        def op(sdk):
            called.append(sdk)
            return "ok"

        # Bypass real SDK build by monkeypatching get_client
        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        result = await client.call_with_retry_async(op, description="get_thing")
        assert result == "ok"
        assert len(called) == 1

    async def test_retries_on_4000_then_succeeds(self, client: SmartsheetClient):
        """Bug 1 fix — Smartsheet errorCode 4000 must trigger retry."""
        attempt = {"n": 0}

        def op(sdk):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise Exception(
                    'Smartsheet response: {"errorCode": 4000, "message": "An unexpected error occurred."}'
                )
            return "ok-after-retry"

        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        result = await client.call_with_retry_async(
            op, description="bug-1-test", max_retries=2, base_delay=0.01
        )
        assert result == "ok-after-retry"
        assert attempt["n"] == 2

    async def test_retries_on_http2_connection_terminated(self, client: SmartsheetClient):
        """Bug 6 — HTTP/2 GOAWAY frame from Supabase pool surfaces during a
        Smartsheet call when usage logging fails. Must be retried."""
        attempt = {"n": 0}

        def op(sdk):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise Exception(
                    "<ConnectionTerminated error_code:1, last_stream_id:937, additional_data:None>"
                )
            return "ok"

        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        result = await client.call_with_retry_async(
            op, description="bug-6-test", max_retries=2, base_delay=0.01
        )
        assert result == "ok"

    async def test_does_not_retry_on_404(self, client: SmartsheetClient):
        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        def op(sdk):
            raise Exception("HTTP 404 Sheet not found")

        with pytest.raises(SmartsheetError, match="404"):
            await client.call_with_retry_async(
                op, description="404-test", max_retries=3, base_delay=0.01
            )

    async def test_401_invalidates_cache_and_retries(
        self, client: SmartsheetClient, rc: RedisClient
    ):
        """When Smartsheet returns 401 the cached token may have been rotated.
        Our client refreshes credentials and retries."""
        await client._resolve_credentials()  # warm cache
        assert await rc.cache_get(client._token_key) is not None

        attempt = {"n": 0}

        def op(sdk):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise Exception("HTTP 401 Unauthorized: invalid token")
            return "ok-after-refresh"

        # Force the SDK build path to go through get_client → _resolve_credentials
        client._cached_sdk_client = MagicMock(name="sdk-stale")

        result = await client.call_with_retry_async(
            op, description="401-test", max_retries=2, base_delay=0.01
        )
        assert result == "ok-after-refresh"
        assert attempt["n"] == 2

    async def test_exhausts_retries(self, client: SmartsheetClient):
        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        attempt = {"n": 0}

        def op(sdk):
            attempt["n"] += 1
            raise Exception("HTTP 502 Bad Gateway")

        with pytest.raises(SmartsheetError, match="failed"):
            await client.call_with_retry_async(
                op, description="exhaust", max_retries=2, base_delay=0.01
            )
        assert attempt["n"] == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_under_limit_allows_concurrent_calls(self, client: SmartsheetClient):
        """5 calls with max=100 — all should succeed without waiting."""
        client._cached_sdk_client = MagicMock(name="sdk")
        client._cached_token = TEST_PLAINTEXT_TOKEN
        client._cached_base_url = TEST_BASE_URL

        async def one_call():
            return await client.call_with_retry_async(
                lambda sdk: "ok", description="rate-test"
            )

        results = await asyncio.gather(*[one_call() for _ in range(5)])
        assert results == ["ok"] * 5

    async def test_exhausted_limit_raises_after_max_wait(
        self, rc: RedisClient, supabase_mock
    ):
        """With max=2 and a 2s window, a 3rd call within the window should
        wait until the window slides forward, but with max_wait=0.1 we hit
        the deadline first."""
        client = SmartsheetClient(
            org_id=TEST_ORG,
            product_slug=TEST_SLUG,
            redis=rc,
            supabase=supabase_mock,
            settings=_test_settings(TEST_KEY_B64),
            rate_limit_window_s=10,
            rate_limit_max_calls=2,
        )
        # Burn the budget
        await rc.rate_limit_consume(
            client._rate_limit_key, window_s=10, max_calls=2
        )
        await rc.rate_limit_consume(
            client._rate_limit_key, window_s=10, max_calls=2
        )

        with pytest.raises(SmartsheetError, match="Rate limit exhausted"):
            await client._wait_for_rate_limit_slot(max_wait_s=0.1)
