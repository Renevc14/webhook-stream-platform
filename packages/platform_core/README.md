# smarthub-core

Shared Python library for the Smarthub platform. Used by all backends and workers.

## Modules

Provider-agnostic core:

- `smarthub_core.crypto` — AES-GCM token encryption (single source of truth for `crypto_helper.py`)
- `smarthub_core.db.redis` — async Redis client with cache-aside, dedup, streams, locks, rate limit helpers, and `atomic_deduct*` / `atomic_release` for bolt caps
- `smarthub_core.db.supabase` — Supabase client with explicit httpx pool and retry
- `smarthub_core.billing` — **opt-in** org-wide bolt metering (`PlatformBudgetReader`, `BoltGate`, `BillingPort`). Postgres usage (via `get_org_billing_usage_total`) is the cycle baseline; Redis `bolt-cap:{org}:consumed|limit|cycle_id` is the shared concurrency gate. Optional `bolt-hold:{org}:{operation_id}` stores the exact held amount (TTL expires metadata only — it does **not** auto-refund the aggregate). Tools that have not imported this package keep their local bolt_cap and are unaffected.
- `smarthub_core.worker` — `SmarthubWorker` base class for stream consumers (retry/backoff, DLQ, optional dedup, opt-in `claim_idle_ms` orphan recovery) + standardized health endpoint
- `smarthub_core.webhooks.secret_cache` — Redis key naming for the webhook shared-secret cache (`webhook-secrets:{org}:{provider}:{tool}`). The gateway is the only writer, but the format is read and asserted on from several repos, so it lives here as a contract instead of being hand-built at each call site

Per-provider modules under `smarthub_core.platforms.{provider}/` — Smartsheet today, HubSpot/Slack/Stripe/Twilio when added:

- `smarthub_core.platforms.shared` — provider-agnostic abstractions:
  - `TokenResolver` interface + `ResolvedToken`
  - `BaseWebhookHandler` + `ChallengeResponse` + `NormalizedEvent`
  - `OAuthRefresher` + `should_refresh()` helper
- `smarthub_core.platforms.smartsheet` — Smartsheet impl:
  - `SmartsheetClient(org_id, product_slug, ...)` with cache-aside token + Lua sliding-window rate limit
  - `SmartsheetTokenResolver` (the credential-resolution engine the client delegates to)
  - `SmartsheetWebhookHandler` — HMAC SHA-256 + sync challenge for the polyglot Webhook Gateway
  - `SmartsheetWebhookService` — webhook CRUD / register / cleanup (SH-193 / SH-207)
  - `SmartsheetPicker` — workspace/sheet picker with 5-minute Redis cache

## Install (editable, from repo root)

```bash
pip install -e ./shared/smarthub_core[dev]
```

## Run tests

```bash
cd shared/smarthub_core
pytest
```

## Conventions

See [`docs/architecture/TARGET_ARCHITECTURE.md`](../../docs/architecture/TARGET_ARCHITECTURE.md):

- Caching: cache-aside, Postgres is source of truth, `DEL` on write
- Tokens in Redis: ciphertext only, ever
- Stream payload: versioned envelope `{v, event_id, tool, org_id, config_id, received_at, raw}`
- Consumer naming: `consumer = f"{tool_slug}-{pod_uuid}"`
- DLQ: `stream:{tool}:dlq` for `FatalError`
