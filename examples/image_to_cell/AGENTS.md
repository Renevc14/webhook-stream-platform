# image-to-cell-3 — active I2C backend (webhooks)

See root `CLAUDE.md`, `@docs/tools/TOOL_REGISTRY.md`, and `backend/image-to-cell-3/` for full context.

## Current sprint context (verify with git/Jira before assuming)

- **PR [#205](https://github.com/ProdactiveSmartHub/smarthub-main/pull/205)** local tip `d824e30` — Pulse cutover + platform write-SoT + BillingPort + session fixes (branch may be ahead of origin; do not push unless asked).
- **Staging ECS:** `stg-image-to-cell:26` — `RUN_LIFECYCLE_POLLER=false`, `WEBHOOK_LIFECYCLE_ENABLED=false`, `RUN_WEBHOOK_RECEIVER=false`, `RUN_STREAM_CONSUMER=true`, `GATEWAY_INTERNAL_URL` + `GATEWAY_M2M_TOKEN`. Ladder `:22`→`:26` (2026-08-03).
- **Ingress:** gateway → `stream:i2c` → stream worker. Create/`register-webhook` → m2m ensure-registration (no Smartsheet register from I2C when lifecycle off).
- **Webhook DB:** `platform_webhook_registrations` write SoT; `imagetocell_webhooks` soft-fail mirror.
- **Billing (charge-on-process):** keep Redis hold on every successful cell write, including reprocess after clear. Skips free; SH-111 `shuffle_no_bill` free. No Redis refund on `ALREADY_LOGGED` after successful write. Staging dropped usage_log UNIQUE indexes (`20260803190000` / applied `20260803231907`).
- **Create backfill:** single config only (`run_for_single_config`). Worksheet `POST /configs/backfill` is ops-wide.
- **Estimate / SH-108 / last_ts:** estimate uses `filter_prior_for_filled_cells`; prune recomputes `last_ts` so Run Backfill does not 0-skip when empty cells need work.
- **Validated post-`:26`:** Redis = usage_log×15; 0 dup refunds; estimate matched processable. Create path not re-smoked under `:26`.
- **Next:** optional create re-smoke → prod promote per `docs/runbooks/WEBHOOK_PLATFORM_CUTOVER.md` §8.
- Progress history: `docs/infrastructure/I2C_PHASE_2_PROGRESS.md`, `I2C_PHASE_3_PROGRESS.md`

## Run commands (Windows — agent shell does not inherit venv)

- Python: `.\backend\image-to-cell-3\venv\Scripts\python.exe`
- Tests: `.\backend\image-to-cell-3\venv\Scripts\python.exe -m pytest backend\image-to-cell-3\tests`
- Dev port: **3011**

## Mode

Webhooks (Pillow, PyMuPDF). Production processing mode: `webhook` (NOT `webhook_v3`).

## Key env vars

`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `APP_CRYPTO_KEY`, `REDIS_URL`,
`SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL`, `GATEWAY_INTERNAL_URL`, `GATEWAY_M2M_TOKEN`,
`ENCRYPT_TOOL_INTERNAL_URL`, `SMARTSHEET_BROKER_SECRET`, `WEBHOOK_PROCESSING_MODES`

**SH-151 runtime roles:** `RUN_HTTP_API`, `RUN_STREAM_CONSUMER`, `RUN_LIFECYCLE_POLLER`,
`RUN_WEBHOOK_RECEIVER`, `WEBHOOK_LIFECYCLE_ENABLED`. Cutover = lifecycle/receiver off, stream on.

## Own / platform tables

- Own: `imagetocell_config`, `imagetocell_dlq`, `imagetocell_usage_log`, `imagetocell_row_state`
- Mirror: `imagetocell_webhooks` (soft-fail)
- Platform SoT: `platform_webhook_registrations`

## Shared tables (read)

`user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`, `organization_subscriptions`

## Product slug

`smart-image-to-cell` — must match `organization_subscriptions` exactly.

## Patterns

- Webhook routes respond in <5s; no heavy work in `Depends()` (direct receiver off on staging)
- Smartsheet writes require `Smartsheet-Change-Agent` header
- Supabase: retry + exponential backoff; use `.limit(1)` not `.single()`
- Scope all queries to `organization_id`
- Billing via `adapters/billing` (`BillingPort`); webhook ensure via `adapters/gateway`

## Documentation on close

When closing a ticket touching this backend, update `docs/PROJECT_STATE.md` and this tool's `CHANGELOG.md`. See `docs/development/DOCUMENTATION_WORKFLOW.md`.
