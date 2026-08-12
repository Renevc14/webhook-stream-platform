# Smarthub — Current State by Tool

> Last updated: 2026-08-04.
> For sprint-level state (PRs, branches, releases in flight), see [docs/PROJECT_STATE.md](../PROJECT_STATE.md).
> For per-tool ports, tables, and env vars, see [docs/tools/TOOL_REGISTRY.md](../tools/TOOL_REGISTRY.md).

This document is a per-tool snapshot of what each backend looks like **today**. It is intentionally narrow in scope: reader wants to answer "what does each backend look like right now?" without having to grep the codebase or chase Jira tickets.

---

## Active backends

There are 9 active FastAPI backends **plus** the shared Webhook Gateway (`services/webhook-gateway/`). The three `image-to-cell*` legacy folders are listed in the Legacy section and must not be modified.

### backend/image-to-cell-3/

- **Purpose**: Converts attachments uploaded to Smartsheet rows into images inside cells (Pillow + PyMuPDF for image/PDF handling).
- **Current prod version**: Pulse cutover **live** — ECS **`prod-image-to-cell:11`**, image **`534f0d7`** (2026-08-04). Staging remains Pulse-shaped (`stg-image-to-cell:26` / `d824e30`).
- **Local / staging port**: **3011**. Prod API: `https://smart-image-to-cell.getsmarthub.co`. Staging: `https://smart-image-to-cell.prodactivelabs.com`.
- **Ingress (prod + staging)**: Smartsheet → **Webhook Gateway** → **`stream:i2c`** → I2C stream consumer. Direct `/webhook/smartsheet` **off** (`RUN_WEBHOOK_RECEIVER=false`).
- **Lifecycle (prod + staging)**: tool poller **off**. Gateway registrar owns Smartsheet webhooks; create/`register-webhook` calls m2m **ensure-registration**.
- **Webhook DB**: write SoT `platform_webhook_registrations`; soft-fail mirror `imagetocell_webhooks`.
- **Billing**: `BillingPort` / `smarthub_core.billing` (fail-closed outside local). **Policy:** charge Redis+Hub on every successful process (incl. reprocess after clear); skips free; SH-111 shuffle free. Prod+staging dropped `imagetocell_usage_log` unique indexes (SH-234 / `20260803190000`).
- **Backfill**: create-config = single config only; estimate shares SH-108 empty-cell prune; `last_ts` recomputed after prune.
- **smarthub_core**: crypto, Redis, Smartsheet client, worker base, callback URLs, platform registrations, billing.
- **Redis**: prod ElastiCache `smarthub-redis-production` + staging ElastiCache — dedup, bolt-cap, streams, secret cache (gateway-owned).
- **Prod task def**: `prod-image-to-cell:11` (2026-08-04, image `534f0d7`). **Staging task def**: `stg-image-to-cell:26` (image `d824e30`).
- **Open / residual**: product smoke on prod (live attachment + SH-234 reprocess) still owed; SmartQR not gateway-owned lifecycle; DROP tool webhook tables later; residual `bolt_cap` for estimate/pause; Hub `get_org_billing_usage_total` deferred on prod.
- **Customer impact**: prod I2C callbacks now route via `webhooks.getsmarthub.co` after registrar re-point; gateway is sole webhook owner for `smart-image-to-cell`.

### services/webhook-gateway/

- **Purpose**: Shared FastAPI ingress — challenge, HMAC, publish to `stream:{tool}`; lifecycle secret sync + registrar.
- **Prod**: ECS **`prod-webhook-gateway:1`**, host `https://webhooks.getsmarthub.co`, port **3020**, image `534f0d7` (2026-08-04).
- **Staging**: ECS `stg-webhook-gateway:7`, host `webhooks.prodactivelabs.com`, port **3020**.
- **Route shape**: `POST /webhook/{provider}/{tool_slug}/{org_id}`.
- **Internal m2m**: `PUT /internal/v1/webhook-secrets/...`, `POST /internal/v1/lifecycle/ensure-registration` (`GATEWAY_M2M_TOKEN`).
- **Flags (prod + staging)**: `PLATFORM_WEBHOOK_READ=true`, registrar on for `smart-image-to-cell` only, broker + M2M wired. SmartQR secret sync read-only from platform (tool-owned registrar).
- **Repo**: `services/webhook-gateway/`.

### backend/bulk-attachment-uploader/

- **Purpose**: Bulk-uploads files into Smartsheet rows (ZIP → rows).
- **Current prod version**: verify in TOOL_REGISTRY / release tags.
- **Local dev port**: 3003 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today. Candidate for `smarthub_core.crypto` migration in the same wave as i2c-3 Phase 1.2.
- **Redis dependency**: Redis state with in-memory fallback. Same ElastiCache cluster (SH-119) target once provisioned.
- **Own tables**: `batr_upload_sessions`, `batr_column_auto_config`, `batr_usage_log`.
- **Implicit coupling**: relies on the webhooks I2C registers in Smartsheet to drive downstream automation after uploads.
- **Recent significant changes (last 60 days)**: none merged this sprint. Verify via `gh pr list --search "bulk-attachment-uploader"`.
- **Open PRs touching this backend**: none known. Verify before assuming.
- **Known limitations**: large ZIPs cause memory pressure.

### backend/cell-history/ (+ cell-history-2 on SH-159 branch)

- **Purpose**: Cell-level change history audit log.
- **Current prod version**: polling service only (verify in TOOL_REGISTRY).
- **Local dev port**: 3014 (polling); **cell-history-2** TBD port on SH-159 branch.
- **smarthub_core adoption**: **Partial on SH-159 branch** — crypto, Supabase retry, webhook HMAC. Local Redis-free Smartsheet shim (documented MVP).
- **Redis dependency**: none in prod polling. **cell-history-2 MVP**: Redis-free, `WORKERS=1` until Pulse Redis lands.
- **Mode (prod today)**: polling every 30 s. **In flight (SH-159)**: webhook mode via `backend/cell-history-2/` + `processing_mode` column.
- **Own tables**: `cell_history_configurations`, `cell_history_status_history`, `cell_history_metrics_*`; SH-159 adds `cell_history_webhooks`, `cell_history_failed_events`.
- **Recent significant changes**: **SH-159** (Akber, branch not merged) — full webhook successor, 143 tests, coexistence filter on legacy service.
- **Open work**: [SH-159](https://smarthub-dev.atlassian.net/browse/SH-159) — merge after code review fixes; migrations on staging/prod pending.
- **Known limitations (polling)**: `/cellhistory` on hot path. **Webhook path (SH-159)**: eliminates `/history` on steady state; rapid same-cell edits coalesced by Smartsheet debounce.

### backend/attachment-counter/

- **Purpose**: Counts attachments per row and updates a Smartsheet column with the count.
- **Current prod version**: includes SH-123 + SH-140 (merged in PR #165 on 2026-06-24). Verify exact tag in TOOL_REGISTRY.
- **Local dev port**: 3005 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today.
- **Redis dependency**: none today.
- **Mode**: polling every 70 s. Sequential processing (enterprise reliability focus).
- **Own tables**: `attachment_counter_configs`, `attachment_counter_row_state`, `attachment_counter_*`.
- **Recent significant changes (last 60 days)**:
  - **PR #165 (SH-123 + SH-140, merged 2026-06-24)** — Attachment-Info poller heartbeat added; removed a broken Supabase RPC call that had been logging errors.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: sequential processing caps throughput; webhook-driven mode awaits Phase 4.2.

### backend/dynamic-ddl/

- **Purpose**: Keeps dynamic dropdown lists in Smartsheet columns in sync with an external source of truth.
- **Current prod version**: verify in TOOL_REGISTRY.
- **Local dev port**: 3007 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today.
- **Redis dependency**: none today.
- **Mode**: polling every 90 s. Up to 3 configs in parallel per org.
- **Rate limit**: 240 calls/min to Smartsheet (~90% of the per-token bucket as currently measured; not verified against an authoritative table — see Known Tech Debt #3).
- **Own tables**: `dynamic_ddl_configs`, `ddl_column_values`.
- **Recent significant changes (last 60 days)**: none known. Verify.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: lists >10K items cause memory pressure.

### backend/textnow/

- **Purpose**: Sends SMS via Telnyx when Smartsheet cells change.
- **Current prod version**: verify in TOOL_REGISTRY.
- **Local dev port**: 3131 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today.
- **Redis dependency**: none today. In-memory 10-min per-org cache for workspaces/worksheets — not shared across instances.
- **Mode**: partial webhook (endpoint exists; registration is manual).
- **Own tables**: `textnow_configurations`.
- **External provider**: Telnyx API.
- **Recent significant changes (last 60 days)**: none known. Verify.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: in-memory cache is not shared across instances; webhook registration is manual.

### backend/contact_book_connects/

- **Purpose**: Nightly sync of Smartsheet users into the `external_contacts` book.
- **Current prod version**: verify in TOOL_REGISTRY.
- **Local dev port**: 3058 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today.
- **Redis dependency**: none today.
- **Mode**: APScheduler nightly at 23:59 UTC.
- **Own tables**: `smartsheet_user_sync_config`, `smartsheet_synced_users`, `external_contacts`, `smartsheet_sync_logs`.
- **Recent significant changes (last 60 days)**: none known. Verify.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: no dependencies on other tools; isolated nightly job.

### backend/id-checker/

- **Purpose**: Shared picker service — fetches workspaces / worksheets / columns from Smartsheet for the frontend.
- **Current prod version**: verify in TOOL_REGISTRY.
- **Local dev port**: 3002 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today. Candidate to be folded into `smarthub_core.platforms.smartsheet.SmartsheetPicker` (the library already exposes that class but id-checker has not been migrated to call it locally — it remains an HTTP microservice).
- **Redis dependency**: none today. In-memory TTL cache.
- **Mode**: synchronous Flask REST API. Consumed by every frontend module.
- **Recent significant changes (last 60 days)**: none known. Verify.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: synchronous Flask; cache not shared across instances; the HTTP-microservice shape adds unnecessary latency vs being a library.

### backend/encrypt-tool/

- **Purpose**: Shared crypto microservice — AES-GCM encrypt/decrypt of Smartsheet OAuth tokens.
- **Current prod version**: verify in TOOL_REGISTRY.
- **Local dev port**: 3051 (see TOOL_REGISTRY).
- **smarthub_core adoption**: **No** today, but the migration target is well-defined: replace with `smarthub_core.crypto`. i2c-3 has already moved off the HTTP service for its own crypto. Other backends still call this service.
- **Redis dependency**: none today.
- **Mode**: synchronous Flask microservice.
- **Recent significant changes (last 60 days)**: none known. Verify.
- **Open PRs touching this backend**: none known. Verify.
- **Known limitations**: every crypto op incurs an HTTP round-trip — pure latency overhead. Slated for deprecation in favour of the library import.

---

## Legacy backends — DO NOT MODIFY

| Folder | Status | Reason |
|---|---|---|
| `backend/image-to-cell/` | Legacy v1 | Replaced by image-to-cell-3 |
| `backend/image-to-cell_2/` | Legacy v2 | Replaced by image-to-cell-3 |
| `backend/image-to-cell-donotuse/` | Deprecated | Do not use |

---

## shared/smarthub_core/

The shared Python library is real and consumed (no longer "Phase 0 pending" as older CLAUDE.md text claimed). It was delivered via PR #131 (SH-9, merged 2026-05-13) and PR #139 (SH-14, merged 2026-05-13). Phase 1 (PR #160, open) significantly expands what i2c-3 consumes from it.

### Modules that exist

```
shared/smarthub_core/
└── src/smarthub_core/
    ├── crypto/                  # AES-GCM encrypt/decrypt, key rotation helpers
    ├── db/
    │   ├── redis/               # Redis client wrapper + in-memory fallback
    │   └── supabase/            # PostgREST client wrapper with retry
    ├── worker/
    │   ├── base/                # BaseWorker lifecycle + health primitives
    │   └── health/              # /healthz + readiness checks
    ├── settings/                # Pydantic settings loader
    └── platforms/
        ├── shared/              # TokenResolver, BaseWebhookHandler, OAuthRefresher, should_refresh
        └── smartsheet/          # SmartsheetClient, SmartsheetTokenResolver,
                                 # SmartsheetWebhookHandler, SmartsheetPicker
```

### Import paths (canonical)

- Provider-agnostic: `smarthub_core.crypto`, `smarthub_core.db.redis`, `smarthub_core.db.supabase`, `smarthub_core.worker.{base,health}`.
- Smartsheet-specific: `from smarthub_core.platforms.smartsheet import SmartsheetClient, SmartsheetTokenResolver, SmartsheetWebhookHandler, SmartsheetPicker`.
- Shared abstractions: `from smarthub_core.platforms.shared import TokenResolver, BaseWebhookHandler, OAuthRefresher, should_refresh`.

### Which backends consume what (today, 2026-06-25)

| Backend | crypto | db.redis | db.supabase | platforms.smartsheet | worker.base/health |
|---|---|---|---|---|---|
| image-to-cell-3 | Yes | No (Phase 1 adds) | No (Phase 1 adds) | No (Phase 1 adds) | No (Phase 1 adds) |
| bulk-attachment-uploader | No | No | No | No | No |
| cell-history | No | No | No | No | No |
| attachment-counter | No | No | No | No | No |
| dynamic-ddl | No | No | No | No | No |
| textnow | No | No | No | No | No |
| contact_book_connects | No | No | No | No | No |
| id-checker | No | No | No | No (could use SmartsheetPicker) | No |
| encrypt-tool | N/A (it _is_ the legacy crypto) | No | No | No | No |

### Phase 1 deepening (PR #160, open against develop)

PR #160 takes i2c-3 from "consumes `crypto` only" to "consumes `crypto + db.supabase + db.redis + platforms.smartsheet + worker.{base,health}`". Specifically:

- `SmartsheetWebhookService` (sync today) is rewritten as an async class living in i2c-3 but delegating list/create/delete/verify calls to `SmartsheetWebhookHandler` from the library.
- Supabase calls flow through `smarthub_core.db.supabase`, which standardises retry with exponential backoff (today the i2c-3 code does this inline).
- Redis usage flows through `smarthub_core.db.redis` with in-memory fallback — same behaviour, single implementation.
- Worker lifecycle (`uvicorn` startup, health endpoint, graceful shutdown) wraps `smarthub_core.worker.base.BaseWorker` and `smarthub_core.worker.health`.

After PR #160 merges, the second backend to migrate is **bulk-attachment-uploader** (crypto + supabase first, then redis), which keeps the rollout to "one backend per deploy" per the migration plan.

---

## Known tech debt (still open, unchanged)

1. `crypto_helper.py` was copied into 6 backends — silent divergence risk until each is migrated to `smarthub_core.crypto`.
2. `id-checker` and `encrypt-tool` are HTTP microservices when they should be library imports. Encrypt-tool's migration path is clear; id-checker's overlaps with `SmartsheetPicker`.
3. Smartsheet rate limiting is per-process, not cluster-wide. Multiple instances of the same tool against the same org can collectively exceed the per-token bucket. Code currently uses ~240–300 req/min as a reference; not verified against an authoritative table.
4. In-memory caches in textnow, id-checker, and i2c-3 are not shared across instances. ElastiCache (SH-119) is the fix.
5. i2c-3 `_db_semaphore` hardcoded to `min(8, max_concurrent_attachments)` — Windows workaround capping Linux throughput.
6. cell-history and attachment-counter use polling when they could react to webhooks in <1s (Phase 4).

---

## Verification

This document is best-effort and reflects the focus of current work (i2c-3 and Phase 1). Everything stated about **i2c-3** is verified against this sprint's PRs and the 1.6.1 release. Everything about the **other 8 backends** should be re-verified before being relied on for a decision — specifically:

- Run `gh pr list --search "<backend-name>" --state all --limit 20` to confirm there are no recently merged PRs the doc missed.
- Cross-check ports, env vars, and own-table names against [docs/tools/TOOL_REGISTRY.md](../tools/TOOL_REGISTRY.md) — that document is the authority on those fields.
- Confirm the prod version of each backend against the actual deploy (ECS task definition `image` tag) rather than trusting any "verify in TOOL_REGISTRY" placeholder in this file.
- For sprint-scoped state (open PRs, branches in flight, release windows), prefer [docs/PROJECT_STATE.md](../PROJECT_STATE.md) and re-run `gh pr list --state open` rather than relying on snapshots in this document.

Anyone updating this file should bump the "Last updated" date at the top and append a short note to PROJECT_STATE.md if a per-tool change is also sprint-relevant.
