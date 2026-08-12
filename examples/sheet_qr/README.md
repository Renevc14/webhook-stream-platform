# SmartQR backend

Catalogue tool: write QR / Code128 / EAN images into Smartsheet cells.

- **Port:** 3021  
- **Slug:** `smart-qr`  
- **Docs:** [SMARTQR_AGREEMENTS.md](../../docs/product/SMARTQR_AGREEMENTS.md) · [SMARTQR_DEV_PLAN.md](../../docs/development/SMARTQR_DEV_PLAN.md) · [SMARTQR_SANDBOX_TEST_PLAN.md](../../docs/development/SMARTQR_SANDBOX_TEST_PLAN.md)

## Local (Docker)

From **repo root**:

```bash
docker compose -f backend/smart-qr/docker-compose.yml up --build
```

- API: http://localhost:3021/health · http://localhost:3021/docs  
- Postgres: localhost:5433 (user/pass/db: `smartqr`)  
- Redis: localhost:6380  

Hub FE: set `REACT_APP_SMARTQR_BACKEND_URL=http://localhost:3021` and run the existing frontend.

## Local (host Python)

```bash
pip install -e ./shared/smarthub_core
pip install -r backend/smart-qr/requirements.txt
cd backend/smart-qr
set PYTHONPATH=src
uvicorn smart_qr.api.app:app --reload --port 3021
```

## Tests

```bash
cd backend/smart-qr
PYTHONPATH=src pytest tests/unit -q
```

## Sandbox

Sandbox points the same image at the Supabase **sandbox** project. Copy
[`.env.sandbox.example`](.env.sandbox.example) to `.env`, fill it from the
sandbox secret store, and follow
[SMARTQR_SANDBOX_TEST_PLAN.md](../../docs/development/SMARTQR_SANDBOX_TEST_PLAN.md).

## Environment

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `local` | Only `local` relaxes auth. Anything else (`sandbox`, `staging`, `production`) requires a JWT and a verifiable org. |
| `PORT` | `3021` | |
| `LOG_LEVEL` | `INFO` | |
| `DATABASE_URL` | local Postgres | Supabase: prefer the direct connection on 5432. |
| `DATABASE_SSL` | `auto` | `auto` = TLS for any non-local host. `require` / `disable` to force. |
| `DATABASE_STATEMENT_CACHE_SIZE` | unset | Auto-zero behind a pgBouncer pooler (host `pooler.*` or port 6543). |
| `REDIS_URL` | `redis://localhost:6380/0` | Required by `/ready` and the stream consumer. |
| `RUN_STREAM_CONSUMER` | `true` | |
| `SUPABASE_URL` | empty | Needed for ES256 (JWKS) tokens and for the membership check below. |
| `SUPABASE_JWT_SECRET` | empty | **Mandatory outside `local`** — without it every request is 401. |
| `SUPABASE_SERVICE_ROLE_KEY` | empty | **Mandatory outside `local`** — verifies `X-Organization-ID` against the Hub's `user_organization_memberships` / `user_profiles`, which RLS hides from our Postgres role. Without it every request is 403. Server-side only. |
| `CORS_ORIGIN` | empty | Comma-separated allowlist. Empty = `localhost:3000` only. |
| `SMARTSHEET_MODE` | `live` | `live` / `mock`. |
| `SMARTSHEET_ACCESS_TOKEN` | empty | Service-wide token for the local/sandbox MVP; OAuth per org comes later. |
| `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL` | empty | Public base of the shared webhook gateway. Required to register a webhook; point a tunnel at the gateway when testing locally. |
| `APP_CRYPTO_KEY` | insecure dev default | Must be overridden outside local. |
| `SMARTQR_BILLING_MODE` | `mock` | Only mock exists today: nothing is charged. |
| `GATEWAY_INTERNAL_URL` / `GATEWAY_M2M_TOKEN` | empty | Push webhook secrets to the gateway when set. |

## Operational surface

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | public | Liveness (Docker/ECS health check). |
| `GET /ready` | public | Postgres + Redis. **503** when Postgres is down. |
| `POST /api/v1/dev/inject-event` | user + org | Local only; refuses to run outside `APP_ENV=local`. |

Known gaps before staging: no `/metrics`, no per-row error persistence, no DLQ
or event dedup, no rate limiting, and billing is mock. Tracked in
[SMARTQR_DEV_PLAN.md](../../docs/development/SMARTQR_DEV_PLAN.md).
