# Image-to-Cell v3

Smartsheet image processing service. Automatically places row attachments into designated cells via webhooks.

## Features

- **Real-time processing** via Smartsheet webhooks
- **Parallel processing** (25 concurrent workers)
- **Auto-recovery** for disabled or failed webhooks
- **Dead Letter Queue** for failed event retry
- **Backfill** on new config creation (processes existing rows immediately)
- **Supported formats**: JPG, PNG, GIF, BMP, TIFF, WEBP, HEIC, PDF

## Quick Start

```bash
# 1. Install the shared platform library (editable) from the repo root.
#    smarthub_core is required at import time
#    (src/services/smartsheet/client.py pulls in smarthub_core.crypto).
#    In Docker it is copied to /opt/smarthub_core; locally you must install
#    it yourself — it is intentionally NOT pinned in requirements.txt.
pip install -e ../../shared/smarthub_core

# 2. Install image-to-cell-3 dependencies.
pip install -r requirements.txt

# 3. Configure environment.
cp .env.example .env
# Edit .env with your credentials

# 4. Run.
python -m uvicorn src.api.app:app --host 0.0.0.0 --port 3011
```

## Dependencies

This service depends on the shared platform library at
[`shared/smarthub_core`](../../shared/smarthub_core/README.md) for crypto,
Supabase/Redis clients, and the platform-agnostic Smartsheet primitives.

## Managed Redis (optional, future)

For **multi-task** deployments or shared rate limits, you may add **Amazon ElastiCache (Redis)** in the same VPC as ECS. A CloudFormation **template** and step-by-step guide live in [`infrastructure/README-ELASTICACHE.md`](infrastructure/README-ELASTICACHE.md). Image-to-Cell v3 does not require Redis today; use that doc when you are ready to implement it in code.

## Configuration

See [`.env.example`](.env.example) for the full list. The variables below are the ones you must set (or that are commonly tuned). Anything not listed here has a sensible default.

### Required

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `APP_CRYPTO_KEY` | AES-256 key for token encryption (consumed via `smarthub_core.crypto`) |
| `PRODUCT_SLUG` | Product identity (e.g. `smart-image-to-cell`); consumed by `smarthub_core` |
| `WEBHOOK_BASE_URL` | Public host base for callbacks |
| `WEBHOOK_CALLBACK_URL` | Full callback URL — typically `{WEBHOOK_BASE_URL}/webhook/smartsheet` |
| `I2C_ADMIN_API_KEY` | Required for `/dlq/*` and `/metrics` endpoints |
| `SUPABASE_JWT_SECRET` (alias: `JWT_SECRET`) | Required when `ENVIRONMENT=production`; fail-closed unless `I2C_JWT_OPTIONAL_IN_PRODUCTION=1` |

### Webhooks

| Variable | Description |
|----------|-------------|
| `WEBHOOK_PROCESSING_MODES` | Comma-separated modes this instance owns. Prod: `webhook`. Local/ngrok isolation: `webhook_v3`. Both: `webhook,webhook_v3` |
| `WEBHOOK_LIFECYCLE_ENABLED` | Enable the periodic lifecycle/health worker (default: `true`) |
| `WEBHOOK_LIFECYCLE_INTERVAL_S` | Health check interval in seconds (default: `300`) |

### Supabase pool (read by `smarthub_core.db.supabase`)

| Variable | Description |
|----------|-------------|
| `SUPABASE_MAX_CONNECTIONS` | httpx pool size (default: `5`) |
| `SUPABASE_MAX_KEEPALIVE` | Max keepalive connections (default: `3`) |
| `SUPABASE_KEEPALIVE_EXPIRY_S` | Keepalive expiry (default: `20`) |
| `SUPABASE_TIMEOUT_S` | Request timeout in seconds (default: `30`) |
| `SUPABASE_MAX_CONCURRENT_OPS` | In-flight Supabase op semaphore (default: `4`) |

### Operator / debugging

| Variable | Description |
|----------|-------------|
| `I2C_LOG_WEBHOOK_BODY` | If `true`, log raw Smartsheet webhook bodies at INFO. Off by default — bodies are 5–30 KB and contain customer-scoped IDs. Turn on only during incidents |
| `I2C_CORS_STRICT` | `1` in production drops `localhost` from the CORS allowlist |
| `CORS_ORIGIN` | Comma-separated extra CORS origins |
| `LOG_LEVEL` / `LOG_JSON_FORMAT` | Logging verbosity and JSON-vs-pretty output |

## API Endpoints

### Health & ops

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness probe with basic metrics |
| `GET /ready` | Readiness probe (DB + config repo) |
| `GET /info` | Build / version / settings summary |
| `GET /metrics` | Admin metrics (requires `I2C_ADMIN_API_KEY`) |

### Webhooks

| Endpoint | Description |
|----------|-------------|
| `POST /webhook/smartsheet` | Smartsheet webhook receiver (HMAC verification + dispatch) |
| `GET /webhook/status` | Last webhook delivery diagnostics |

### Dead Letter Queue (admin)

| Endpoint | Description |
|----------|-------------|
| `GET /dlq/stats` | Aggregate DLQ statistics |
| `GET /dlq/events` | List pending failed events |
| `POST /dlq/events/{event_id}/retry` | Manually retry a stuck DLQ event |
| `POST /dlq/events/{event_id}/resolve` | Mark a DLQ event resolved |
| `DELETE /dlq/cleanup` | Purge old DLQ events |

### Configurations

| Endpoint | Description |
|----------|-------------|
| `GET /configs` | List configurations |
| `POST /configs` | Create a configuration |
| `GET /configs/{config_id}` | Get a single configuration |
| `PUT /configs/{config_id}` | Update a configuration |
| `DELETE /configs/{config_id}` | Delete a configuration |
| `POST /configs/{config_id}/register-webhook` | Force webhook (re-)registration |
| `POST /configs/backfill` | Manually trigger backfill for a worksheet |
| `GET /configs/{config_id}/stats` | Per-config processing stats |

### Smartsheet metadata (used by the config UI)

| Endpoint | Description |
|----------|-------------|
| `GET /workspaces` | List workspaces |
| `GET /workspaces/{workspace_id}/worksheets` | List worksheets in a workspace |
| `GET /workspaces/{workspace_id}/worksheets/{worksheet_id}/columns` | List columns for a worksheet |

### Manual trigger

| Endpoint | Description |
|----------|-------------|
| `POST /manual-trigger` | Force reprocess of a single row |

## Architecture

```
Smartsheet Webhook → FastAPI → Parallel Processor → Cell Image Upload
                                      ↓
                              DLQ (on failure)
```

## Background Services

- **Webhook Lifecycle Manager**: Periodic health checks and auto-recovery for disabled webhooks. Lists webhooks via explicit pagination (`page=1..N`, `page_size=100`) following Smartsheet's deprecation of `includeAll`
- **DLQ Processor**: Retries failed events (max 3 attempts). Supports both per-attachment (`event_data.attachment_id`) and row-batch (`event_data.attachment_ids`) event shapes — see `src/workers/dlq_processor.py::_retry_attachment_event`

## Processing Modes

Configurations support three processing modes:

| Mode | Handled by | Description |
|------|-----------|-------------|
| `webhook` | v2 | Real-time via Smartsheet webhooks (v2 managed) |
| `webhook_v3` | v3 | Real-time via Smartsheet webhooks (v3 managed) |
| `polling` | v2 | Periodic polling fallback |

### `webhook_v3` mode

This mode was introduced to allow v3 to run independently alongside a deployed v2 instance sharing the same database. Configurations set to `webhook_v3` are **ignored by v2's auto-recovery process**, which only queries for `webhook` mode. This prevents v2 from overwriting webhook registrations managed by v3 (e.g., when testing with a local ngrok URL).

When deploying v3 as the sole backend, both `webhook` and `webhook_v3` can be treated as equivalent. The frontend creates all new configurations as `webhook_v3` by default.

## Webhook Registration Notes

- The webhook lifecycle manager detects new configurations and registers webhooks automatically
- `register_webhook` runs in a thread pool executor to keep the event loop free, allowing Smartsheet's verification challenge to be answered immediately
- Zombie cleanup only considers webhooks pointing to the configured `WEBHOOK_CALLBACK_URL` — webhooks from other deployments are never touched

## Logs

```bash
python -m uvicorn src.api.app:app --host 0.0.0.0 --port 3011 2>&1 | tee "logs_$(date +%Y%m%d_%H%M%S).txt"
```
