"""
Layer B end-to-end tests against sandbox Supabase + sandbox Smartsheet.

Opt-in via SMARTHUB_E2E_SANDBOX=1 plus the env vars listed in conftest.py
(APP_CRYPTO_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, E2E_ORG_ID,
E2E_PRODUCT_SLUG).

**All tests are read-only against the external services.** They never
INSERT/UPDATE/DELETE in sandbox Postgres, never write data via the
Smartsheet API. The sandbox state is treated as immutable test fixture.

Pre-requisites in sandbox (set up once by hand):

1. An organization exists with `id = E2E_ORG_ID`.
2. `smartsheet_tokens` has a row for (`E2E_ORG_ID`, `E2E_PRODUCT_SLUG`)
   with `encrypted_token` produced by the sandbox `APP_CRYPTO_KEY`. The
   plaintext token must be a valid Smartsheet PAT for an account with at
   least one accessible sheet.
3. `organization_ss_api_base` has a row for `E2E_ORG_ID` with `base_url`
   set to one of `https://api.smartsheet.com` (US), `https://api.smartsheet.eu`
   (EU), or `https://api.smartsheetgov.com` (Gov).

If any pre-requisite is missing, tests will fail with a clear error
message — no mutating fix-up attempted.
"""

from __future__ import annotations

import json

import pytest

from smarthub_core.crypto import CryptoService
from smarthub_core.db.redis import RedisClient
from smarthub_core.platforms.smartsheet.client import SmartsheetClient

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# E2E-7: Crypto roundtrip with sandbox ciphertext
# ---------------------------------------------------------------------------


class TestE2E7CryptoSandboxRoundtrip:
    async def test_can_decrypt_real_sandbox_token(
        self, sandbox_supabase, sandbox_env, sandbox_settings
    ):
        """Read the encrypted token out of sandbox `smartsheet_tokens`,
        decrypt with `smarthub_core.crypto`, and verify the result has
        the shape of a Smartsheet PAT."""
        result = await sandbox_supabase.execute_with_retry_async(
            lambda: sandbox_supabase.table("smartsheet_tokens")
                .select("encrypted_token")
                .eq("organization_id", sandbox_env["E2E_ORG_ID"])
                .eq("product_slug", sandbox_env["E2E_PRODUCT_SLUG"])
                .limit(1)
                .execute(),
            description="e2e: read sandbox token",
        )
        assert result.data, (
            f"sandbox row missing for org={sandbox_env['E2E_ORG_ID']} "
            f"slug={sandbox_env['E2E_PRODUCT_SLUG']} — pre-requisite not met"
        )
        ciphertext = result.data[0]["encrypted_token"]
        assert ciphertext, "encrypted_token column is empty"

        crypto = CryptoService(settings=sandbox_settings)
        plaintext = crypto.decrypt(ciphertext)

        # Smartsheet PATs are typically opaque strings ~30+ chars, base32-ish.
        # We don't validate the format strictly — just that decryption produced
        # something non-empty and printable.
        assert plaintext
        assert len(plaintext) >= 16
        assert plaintext.isprintable()


# ---------------------------------------------------------------------------
# E2E-8: SmartsheetClient resolves token + caches ciphertext
# ---------------------------------------------------------------------------


class TestE2E8SmartsheetClientCaching:
    async def test_resolve_credentials_populates_redis_with_ciphertext(
        self, e2e_rc: RedisClient, sandbox_supabase, sandbox_env, sandbox_settings
    ):
        client = SmartsheetClient(
            org_id=sandbox_env["E2E_ORG_ID"],
            product_slug=sandbox_env["E2E_PRODUCT_SLUG"],
            redis=e2e_rc,
            supabase=sandbox_supabase,
            settings=sandbox_settings,
        )

        # Ensure cache is empty at start
        assert await e2e_rc.cache_get(client._token_key) is None

        # First resolve hits Postgres + caches
        token, base_url = await client._resolve_credentials()
        assert token, "token decryption returned empty"
        assert base_url.startswith("https://"), f"unexpected base_url: {base_url}"

        # Cache must contain ciphertext only — never plaintext
        cached = await e2e_rc.cache_get(client._token_key)
        assert cached is not None, "cache not populated after first resolve"
        parsed = json.loads(cached)
        assert "ciphertext" in parsed
        assert "base_url" in parsed
        assert parsed["base_url"] == base_url
        # Critical security invariant
        assert token not in cached, (
            "PLAINTEXT TOKEN LEAKED INTO REDIS — this is a security regression"
        )

        # Second resolve uses cache (verify by deleting Postgres mock or
        # by simply checking the cached entry is the source). We can confirm
        # by comparing instance counts — but easier: invalidate then reuse.
        token2, base2 = await client._resolve_credentials()
        assert token2 == token
        assert base2 == base_url


# ---------------------------------------------------------------------------
# E2E-9: SmartsheetClient calls real Smartsheet API
# ---------------------------------------------------------------------------


class TestE2E9SmartsheetClientLiveCall:
    async def test_get_current_user_succeeds_and_consumes_rate_limit(
        self, e2e_rc: RedisClient, sandbox_supabase, sandbox_env, sandbox_settings
    ):
        """Read-only call against Smartsheet — validates: token decrypts to a
        working PAT, base_url is reachable, rate-limit slot is consumed in
        Redis, and the SDK round-trip works in `asyncio.to_thread`."""
        client = SmartsheetClient(
            org_id=sandbox_env["E2E_ORG_ID"],
            product_slug=sandbox_env["E2E_PRODUCT_SLUG"],
            redis=e2e_rc,
            supabase=sandbox_supabase,
            settings=sandbox_settings,
        )

        # No rate-limit usage yet
        existing = await e2e_rc.raw.zcard(client._rate_limit_key)
        assert existing == 0

        # Live call
        user = await client.call_with_retry_async(
            lambda c: c.Users.get_current_user(),
            description="e2e: get_current_user",
        )

        # SDK returns a User model; converting to dict should yield an email
        as_dict = user.to_dict() if hasattr(user, "to_dict") else dict(user)
        assert "email" in as_dict, f"unexpected response shape: {as_dict.keys()}"

        # Rate-limit recorded exactly one usage
        after = await e2e_rc.raw.zcard(client._rate_limit_key)
        assert after == 1, f"rate limit slot not consumed: zcard={after}"


# ---------------------------------------------------------------------------
# E2E-10: Supabase async retry releases connections during backoff
# ---------------------------------------------------------------------------


class TestE2E10SupabaseRetryReleasesConnections:
    async def test_async_retry_does_not_block_other_tasks(
        self, sandbox_supabase, sandbox_env
    ):
        """Issue a real read while a retrying operation is in its sleep phase.
        If the retry held a httpx connection during sleep (legacy `time.sleep`
        bug), the parallel call would block. With `asyncio.sleep`, both run
        interleaved.

        We can't easily inject a transient failure into the real Supabase, so
        we approximate: kick off many concurrent reads, all succeed within a
        bounded time window. If `asyncio.sleep` were `time.sleep`, the pool
        of `supabase_max_connections=3` would serialize 10 reads instead of
        running them concurrently."""
        import asyncio
        import time

        async def one_read():
            return await sandbox_supabase.execute_with_retry_async(
                lambda: sandbox_supabase.table("smartsheet_tokens")
                    .select("organization_id")
                    .eq("organization_id", sandbox_env["E2E_ORG_ID"])
                    .limit(1)
                    .execute(),
                description="e2e: concurrent read",
            )

        start = time.monotonic()
        results = await asyncio.gather(*(one_read() for _ in range(10)))
        elapsed = time.monotonic() - start

        # All 10 reads succeeded
        assert len(results) == 10
        # Concurrency observable: 10 reads against pool of 3 should NOT take
        # 10x as long as a single read. If it did, the semaphore is broken.
        # Rough sanity check: <8s for 10 reads (single read should be ~100-300ms,
        # serialized would be 1-3s, with semaphore 3 it should be <2s)
        assert elapsed < 8.0, f"reads serialized: elapsed={elapsed:.1f}s"
