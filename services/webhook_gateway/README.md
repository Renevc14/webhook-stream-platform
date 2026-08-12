# Webhook Gateway (SH-172 + SH-153)

Shared FastAPI ingress for Smartsheet webhooks (v1). After HMAC verification it XADDs one versioned envelope per callback to `stream:{tool}` (`smart-image-to-cell` → `stream:i2c`); the tool's embedded consumer (SH-152) does the heavy processing. A failed XADD returns 503 so Smartsheet re-delivers — consumer dedup absorbs the at-least-once delivery.

## Local dev

```powershell
pip install -r services/webhook-gateway/requirements.txt
pip install -e ./shared/smarthub_core
$env:PYTHONPATH = "services/webhook-gateway/src"
.\backend\image-to-cell-3\venv\Scripts\python.exe -m uvicorn gateway.main:app --reload --port 3020
```

## Local E2E (Docker, full Phase 3 path)

```powershell
docker compose -f docker-compose.phase3.yml up --build -d   # redis + gateway + i2c monolith
..\..\backend\image-to-cell-3\venv\Scripts\python.exe ..\..\backend\image-to-cell-3\scripts\sh153_gateway_e2e.py
```

Validated 2026-07-15: challenge 15ms · signed callback → 200 in 47ms → `stream:i2c` → consumed + acked by `i2c-workers` · invalid signature and unknown org → 401, stream untouched.

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (Redis ping when `REDIS_URL` set) |
| POST | `/webhook/{provider}/{tool_slug}/{org_id}` | Provider dispatch |

## Env vars

| Variable | Required | Default |
|----------|----------|---------|
| `REDIS_URL` | Staging/prod | — |
| `LOG_LEVEL` | No | `INFO` |
| `ENVIRONMENT` | No | `development` |
| `GATEWAY_PORT` | No | `3020` |
| `GATEWAY_TOOL_REGISTRY_JSON` | No | — | JSON array overlay for `ToolRegistry` (SH-228). Built-ins cover I2C + SmartQR. |
| `WEBHOOK_REGISTRAR_TOOLS` | No | `smart-image-to-cell` | Enablement allowlist only (not schema). |
| `RUN_WEBHOOK_REGISTRAR` | No | `false` | Gateway-owned register/renew/zombie. |

Webhook shared secrets are read from Redis cache key `webhook-secrets:{org_id}:{provider}:{tool_slug}` (JSON map `{webhook_id: secret}`). Populated by the lifecycle manager (SH-173). The gateway is the only writer; the format is owned by `smarthub_core.webhooks.secret_cache` so the tools that read the same contract cannot drift from it (SH-215). A tool that needs a secret cached before the next sync cycle pushes it over the internal m2m route rather than writing Redis itself.

**N-tool onboarding (SH-227):** add a registry overlay entry (tables + `stream_name` + provider) via `GATEWAY_TOOL_REGISTRY_JSON`, implement a stream consumer, then optionally list the slug in `WEBHOOK_REGISTRAR_TOOLS` after the tool's own lifecycle poller is off. Unknown URL slugs return **404** (no orphan streams). New webhook tables must follow [WEBHOOK_REGISTRATION_SCHEMA.md](../../docs/development/WEBHOOK_REGISTRATION_SCHEMA.md).

## SH-173 — lifecycle secret cache

The gateway lifecycle manager (singleton via `lock:lifecycle:manager`, TTL 600s /
heartbeat 60s) loads active rows from `imagetocell_webhooks`, decrypts
`shared_secret` with `APP_CRYPTO_KEY`, and writes:

```
webhook-secrets:{org_id}:smartsheet:smart-image-to-cell  ->  {webhook_id: plaintext_secret}
```

TTL default 300s. Env knobs: `RUN_LIFECYCLE_POLLER`, `WEBHOOK_LIFECYCLE_INTERVAL_S`,
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `APP_CRYPTO_KEY`.

Local validation (Docker + sandbox):

```
docker compose -f docker-compose.phase3.yml up --build -d
# Redis published on host 6380 in this compose to avoid clashing with other local Redis
REDIS_URL=redis://localhost:6380/0 python ../../backend/image-to-cell-3/scripts/sh173_secret_sync_e2e.py
```

Unit: `pytest tests/unit/test_sh173_*.py`

Full Smartsheet register/renew/zombie port from I2C remains a follow-up on this
ticket; I2C keeps `RUN_LIFECYCLE_POLLER` for registration until that lands.
