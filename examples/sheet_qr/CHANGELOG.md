# SmartQR backend changelog

## [Unreleased]

### Fixed — prod SmartQR billing unblock (2026-08-04)
- Prod Supabase now has `smart-qr` in `pricing_list` (1 bolt per useful write) and Hub billing inclusion RPCs (`get_org_billing_usage_total`, overage/snapshot/invoice). Unblocks Generate `product_not_priced` / missing cycle baseline on prod.

### Changed — SH-230 D platform webhook write SoT
- `WebhookRepository.upsert` / `delete_for_worksheet` write `platform_webhook_registrations` first (required, tool_slug `smart-qr`); `smartqr_webhooks` soft-fail mirror until table drop. Uses `smarthub_core.webhooks.platform_registrations` SQL constants. Tests: `test_sh230d_platform_webhook_write.py`.

### Changed — billing Redis outage fails closed outside local
- `smarthub_core.billing.BoltGate.reserve` no longer falls open when Redis is down unless `allow_when_unknown=True` (local). Staging/prod refuse with `budget_unavailable` so tools cannot overspend a shared Postgres snapshot. Tests updated in `test_bolt_gate.py`.

### Added — Custom link (`content_mode=advanced`) API + FE (2026-08-03)
- Expose the existing template payload builder as a first-class mode: create/update accept `content_mode=advanced` with a non-empty `template` (`{Column Name}` placeholders). QR-only; `form_base_url` is cleared.
- `process_row` builds the payload via `build_advanced_template` when `content_mode=advanced` (no longer triggers on a stray `template` under other modes).
- Migration `012_advanced_content_mode.sql` adds `advanced` to the `smartqr_configs.content_mode` check. The `template` column was already present from init.
- Hub wizard: Prefill Form removed from new-mode selector (legacy configs keep working); **Custom link** replaces that slot.
- Unit coverage: advanced payload builder + `process_row` happy path (template URL hash).

### Fixed — SH-233 regenerate replaces prior printable attachment (2026-07-31)
- After a style/logo change, Generate rewrote the cell image but left the previous `smartqr-*.png` row attachment, so sheets accumulated files across design rounds.
- Cause: `clear_for_config` DELETE'd `smartqr_row_states`, dropping `attachment_id`, so `_replace_row_attachment` had nothing to delete.
- Fix: invalidate `payload_hash` / `attachment_payload_hash` only (keep `attachment_id`); before upload, delete the tracked id **and** any row file with our deterministic filename. User attachments with other names are untouched.

### Fixed — SH-232 row attachment Change-Agent crash (2026-07-31)
- Staging backfills with `attach_to_row=true` wrote the cell image then failed the row attachment with `ImportError! Could not load api or model class default_headers` / `'str' object does not support item assignment`.
- Cause: `upload_row_attachment` / `delete_row_attachment` / `update_cell_value` assigned into `client.default_headers`. The Smartsheet Python SDK has no such dict — `__getattr__` returns the string `"default_headers"`.
- Fix: set Change-Agent via `client.with_change_agent(...)` (SDK-supported; `prepare_request` injects the header). Helper `_apply_change_agent` guards the string trap.
- Coverage: unit regression + `scripts/sh232_attachment_smoke.py` (sdk crash repro + live attach/list/replace/delete).

### Added — optional printable row attachments (2026-07-30)
- Cell-image output remains mandatory. New configurations can opt into **“Also attach to the row (recommended for printing)”**; the checkbox defaults off and Smartsheet's existing individual/bulk attachment workflow remains the printing surface.
- The rendered PNG is reused byte-for-byte and named `smartqr-{column_id}-{row_id}.png`. The second destination never creates a second usage event or bolt charge.
- `smartqr_row_states` records the owned Smartsheet `attachment_id` and its payload hash. Regeneration replaces only that file—never user-managed attachments. If the cell succeeds but the attachment fails, the durable charge and cell hash remain; the next attempt repairs only the attachment without rewriting the cell or rebilling.
- Migration 011 adds `attach_to_row`, attachment state, and a unique index on `(organization_id, worksheet_id, target_column_id)`. The API already enforced one config per target column; the index closes the concurrent-create race and the API maps it to HTTP 409.
- Unit suite: **143 green**, including attachment success, partial-failure repair, ownership-safe replacement, and SDK operations through the shared core retry envelope.

### Changed — shared bolt metering via `smarthub_core.billing` (2026-07-30)
- **Billing lives in core.** `adapters/billing/` is a thin re-export / build path over `smarthub_core.billing` (`PlatformBudgetReader`, `BoltGate`, `BillingPort`). Private Lua and the ad-hoc `redis.asyncio` client are gone; reservations use `RedisClient.atomic_deduct_with_cycle` / `atomic_release` on the shared core Redis from `get_core_redis`.
- **Cycle baseline is real usage.** `OrgBudget.consumed_bolts` comes from `get_org_billing_usage_total` (required `usage_rpc`), not `get_subscription_status.total_bolts_consumed`.
- **Exact release.** `Reservation.bolts` is the held amount; `process_row` releases that amount (and optional `operation_id` hold metadata). Hold TTL does not auto-decrement the aggregate consumed counter.
- **Mock fails fast outside local.** `SMARTQR_BILLING_MODE=mock` raises at boot in non-local envs instead of only logging. Shutdown closes the platform reader through `reset_billing`.
- **Idempotent usage + stream error surfacing.** `smartqr_usage_log.operation_key` (unique per org) prevents double charge on reclaim/`force` replay; duplicate inserts refund the Redis hold. `SmartQRStreamWorker` now retries / DLQs `ProcessResult(status=error)` instead of ACKing silent failures.
- Unit tests: `tests/unit/test_bolt_gate.py` updated; core coverage in `shared/smarthub_core/tests/test_billing.py`.

### Removed — SH-215 / SH-206 SmartQR stops receiving webhooks (2026-07-30)
SmartQR is not in production yet, so instead of carrying a migration path it now starts where the platform is going: **the gateway is the only webhook receiver, and SmartQR only consumes `stream:smart-qr`.** Four things went away as a result.

- **The direct route `POST /webhook/smartsheet/{org_id}` and its `RUN_WEBHOOK_RECEIVER` flag.** `workers/stream_worker.py` already did everything the route did, with retries, a DLQ and orphan reclaim that the inline handler never had. Keeping both meant two ingress paths, two HMAC verifiers and a flag to keep them apart. Staging webhooks must be re-registered against the gateway base; there is no fallback URL.
- **SmartQR's secret cache.** With no receiver there is no HMAC to verify, so `WebhookRepository.secrets_map_for_org` and the `smartqr:webhook-secrets:{org}` key are gone, along with the write-through on `upsert` (which no longer takes `shared_secret_plaintext`). The gateway owns `webhook-secrets:{org}:{provider}:{tool}` alone and fills it by decrypting `smartqr_webhooks` itself. The pre-SH-215 key SmartQR used to write with a 24h TTL is no longer written by anything and expires on its own.
- **The legacy XOR dual-read.** `decrypt_webhook_secret` had no caller left once the cache went; `shared_secret_encrypted` is written with `smarthub_core.crypto` AES-GCM and read only by the gateway. Any row still holding XOR ciphertext is fixed by re-registering, which staging needs anyway for the URL.
- **`WEBHOOK_PUBLIC_BASE_URL`.** It was a second name for `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL` and its name implied SmartQR was the public receiver, which is now false. One variable, required to register: the public base of the gateway (point a tunnel at the gateway for local work). Compose files and the ECS template updated.

- **Secret push kept:** `PUT /internal/v1/webhook-secrets/smartsheet/smart-qr/{org}`, because Smartsheet can deliver the first callback seconds after ENABLED, well inside one gateway sync cycle. It is a cold-start optimisation, not a source of truth.
- Unit tests: `tests/unit/test_sh215_secret_cache_ownership.py` (guards against a receiver or a second cache writer coming back), `tests/unit/test_sh209_webhook_gateway_contract.py`.

### Changed — SH-212 / SH-206 stream consumer on SmarthubWorker (2026-07-30)
- **The hand-rolled XREADGROUP loop is gone.** `workers/stream_worker.py` defines `SmartQRStreamWorker(SmarthubWorker)` on `stream:smart-qr` / group `smart-qr-workers`, so SmartQR now shares I2C's failure semantics: transient row failures retry with exponential backoff (3 attempts) and then land in `stream:smart-qr:dlq` with reason + traceback instead of accumulating in the pending list; malformed entries go straight to DLQ and are acked.
- **Orphan recovery kept and moved into the shared base.** `claim_idle_ms=60000` reclaims entries abandoned by a crashed pod. No envelope-level dedup key is used: it would outlive the reclaim window and make reclaimed messages ack without processing. `ProcessRowService` row-hash dedup still absorbs replays.
- `workers/stream_consumer.py` is now just start/stop + dependency construction, and it uses the shared `smarthub_core` Redis client (`adapters/core_infra.get_core_redis`) instead of an ad-hoc `redis.asyncio` connection. Manual XADD entries (`org_id`/`config_id`/`row_id`, no `v`) still work for smoke scripts.
- Unit tests: `tests/unit/test_sh212_stream_worker.py`.

### Changed — SH-209 / SH-206 gateway webhook contract (2026-07-30)
- **Callback URL includes the tool slug.** Register now builds `{base}/webhook/smartsheet/smart-qr/{org_id}` via `smarthub_core.platforms.smartsheet.resolve_webhook_callback_url`. Existing Smartsheet webhooks with the old 2-segment path must be re-registered. (Superseded below: the env var is now only `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL`.)
- **Webhook secrets use platform AES-GCM.** `shared_secret_encrypted` is written with `smarthub_core.crypto.CryptoService` (same `APP_CRYPTO_KEY` as I2C/gateway). Local default `APP_CRYPTO_KEY` is a valid base64 32-byte key. (Superseded below: the XOR dual-read on decrypt was removed with the rest of the receiver.)
- Unit tests: `tests/unit/test_sh209_webhook_gateway_contract.py`.

### Validated — first real generation on staging through OAuth (2026-07-30)
- A 7-row backfill completed with 7× `cellimages OK` and no WARNING or ERROR in `/ecs/smarthub-stg/smart-qr`. Credentials resolved `source=oauth oauth_state=active region=US` per (org, user) on every row, and the `smart-qr` token in `smartsheet_oauth_tokens` now carries `{READ_SHEETS, WRITE_SHEETS, ADMIN_WEBHOOKS}` with `status=active` and `refresh_failure_count=0` — the missing write scope is closed.
- Billing reconciles exactly: 7 uploads, 7 `smartqr_usage_log` rows at 1 bolt each, all `status=success`, none missing `user_id`, and 7 `smartqr_row_states`. No double charge and no reservation left held.
- The refactor is confirmed running, not merely deployed: boot logs the core resolver against the broker, core's Redis connects to the shared staging ElastiCache (so the org rate-limit window is shared with Image-to-Cell), and the `cellimages` POST now appears in the Smartsheet SDK log, which only happens because the upload goes through core's envelope instead of a private httpx client.
- Still unexercised on staging: `estimate-backfill` from the wizard and a webhook registered against a real sheet.

### Changed — Smartsheet path now uses smarthub_core (2026-07-29)
- **Credential resolution and Smartsheet retries/rate-limit are no longer reimplemented in SmartQR.** `adapters/smartsheet/credentials.py` is a thin translation of `smarthub_core.platforms.smartsheet.SmartsheetTokenResolver` into SmartQR's typed errors (`CredentialsMissing` / `CredentialsRevoked` / `CredentialsTemporarilyUnavailable`). `LiveSmartsheet` and the Hub browse routes call through `SmartsheetClient.call_with_retry_async`, so SmartQR shares the platform's retry classification and the org-wide Redis rate-limit key (`ratelimit:ss-api:{org}:{slug}`) with Image-to-Cell. `cellimages` uploads use the SDK `fresh_operation` path inside that envelope (I2C pattern) instead of a second httpx client.
- New `adapters/core_infra.py` owns the only construction of core Redis / token resolver / Smartsheet clients for this process. Local `SMARTSHEET_ACCESS_TOKEN` still works: every mode of SmartQR's `CredentialResolver` is presented to core through a single adapter, so there is no second way of learning credentials.
- Core gained `TokenTemporarilyUnavailableError` for the case "broker configured but cannot answer, and no PAT exists". Without that third state, an OAuth-only tool (SmartQR) reported a broker blip as "not connected" and told a correctly-connected user to reconnect.
- Unit suite: **107** green. The previous home-grown `_with_retries` / `_is_retryable` / httpx cellimages loop / browse retry loop are gone.

### Fixed — Hub CORS preflight (2026-07-29)
- **`X-Request-Id` is now in the CORS allowlist.** The Hub client stamps every call with that correlation header; Starlette rejected the preflight with 400 and the browser surfaced a bare `Failed to fetch` on Create configuration. Staging never saw the POST.

### Fixed — cellimages authorization failures are not retried (2026-07-29)
- **`403` / `errorCode 1004` is a permission verdict, not a transient blip.** The upload path retried it three times per row, so an under-scoped OAuth token turned a 9-row backfill into 27 doomed calls over 65 seconds and reported `cellimages failed after retries: None`, hiding the cause. Authorization refusals now fail on the first response with the Smartsheet body and an explicit "cannot write images to this sheet (missing WRITE_SHEETS consent or edit access)". Transient `429` / `5xx` / `4004` still retry, and an exhausted retry loop now reports the last response instead of `None`.
- Found on staging: SmartQR's OAuth token held only `READ_SHEETS` because `smart-qr` was absent from the broker's `TOOL_SCOPES`. Bolts behaved correctly throughout — every failed row released its reservation, so `smartqr_usage_log` recorded nothing and no bolts were charged.

### Deployed — staging (2026-07-29)
- SmartQR is live at `https://smart-qr.prodactivelabs.com` on ECS Fargate (`stg-smart-qr:2`, desired/running `1/1`) behind the shared staging ALB. Target health uses `/ready`; public `/health` and `/ready` return 200 and anonymous API access returns 401.
- Staging uses Supabase's project-assigned IPv4 session pooler (`aws-1-eu-west-2.pooler.supabase.com:5432`). The direct database hostname is IPv6-only and cannot be reached from the current IPv4 ECS subnets; pooler shard names must be read from Supabase rather than inferred.
- Gateway M2M secret synchronization is enabled: the shared Gateway and SmartQR receive the same generated `GATEWAY_M2M_TOKEN`; Gateway rolled to `stg-webhook-gateway:2`.
- Provisioned ECR, Secrets Manager, CloudWatch logs, ECS cluster/service, target group, ALB host rule, Route53 alias, migrations 001–009, and active `pricing_list` version (`smart-qr`, 1 bolt).

### Added — OAuth + real bolts (2026-07-29, staging prep)
- **Smartsheet credentials are resolved per (org, user) through encrypt-tool.** Originally via a local `BrokerCredentialResolver`; since the smarthub_core adoption entry above, that path is the shared `SmartsheetTokenResolver` with SmartQR's typed error mapping kept on top. Outside `APP_ENV=local` there is no other source — `SMARTSHEET_ACCESS_TOKEN` is ignored.
- `encrypt-tool` registers `smart-qr` in `TOOL_SCOPES` with `READ_SHEETS` + `WRITE_SHEETS` + `ADMIN_WEBHOOKS`.
- Browse endpoints use `require_browse_auth`: when the Hub pickers omit `X-Organization-ID`, the caller's default org is taken from `user_profiles` via the service role.
- Interactive generate/backfill and background webhook/stream work attribute writes to `as_user_id` or `cfg.runner_user_id` respectively, so the broker always resolves a real user.
- **Real bolt metering (`SMARTQR_BILLING_MODE=bolts`).** `adapters/billing/bolt_gate.py` reads the plan (`get_subscription_status`, suspended / pending payment / hard-cap flags) and the price (`pricing_list.primary_event_bolts` for `product_slug=smart-qr`) over PostgREST with the service role, then reserves on the shared Redis counter `bolt-cap:{org}:*` with the same Lua contract as I2C. Free plans stop at their allocation; paid plans without a hard cap may overage. A missing/inactive price refuses with `product_not_priced` rather than inventing a number. Redis outage still allows work Postgres already approved; an unreadable plan fails closed outside local.
- Interactive routes preflight the budget before generating anything; `estimate-backfill` returns `bolts_per_code`, `estimated_bolts`, `bolts_remaining` and a blocked reason the wizard can show.
- Migration `009_usage_user_attribution.sql` adds `user_id` to `smartqr_usage_log`; successful writes record the acting user and the bolts actually held.
- Staging task definition: `SMARTQR_BILLING_MODE=bolts`, `ENCRYPT_TOOL_INTERNAL_URL`, `SMARTSHEET_BROKER_SECRET` (secret), health probe on `/ready`. No more `SMARTSHEET_ACCESS_TOKEN` in the staging secret.

### Fixed — staging hardening (2026-07-29)
- Smartsheet row reads use `Sheets.get_row` instead of fetching the whole sheet; column titles are cached per `(sheet, token)` for 5 minutes. Transient failures (429 / 5xx / errorCode 4001–4004) retry with backoff; permanent ones fail immediately.
- Stream consumer reclaims idle pending entries every 15s via `XAUTOCLAIM`, so a crashed handle does not leave webhooks stranded after a deploy. Content-level dedup (`hash_unchanged`) means a reclaim of a row that already succeeded is a no-op charge-wise.

### Validated — sandbox (2026-07-29)
- Ran Fase 9a against Supabase **Developer Sandbox**: migrations 001–008 applied, RLS confirmed on all `smartqr_*` tables, `scripts/sandbox_smoke.py` exit 0 with real JWT (foreign org → 403), Identify / Prefill / Open Row generation, idempotent backfill, empty-row skip, image-source rejection, live preview, style rewrite, webhook challenge in 2 ms, signed webhook write path, delete-last-config unregisters Smartsheet webhook.
- **Platform note (resolved below):** a non-`BYPASSRLS` Postgres role cannot read `user_organization_memberships` under current Hub RLS (`auth.uid()`), so the membership check saw zero rows and returned false 403s. Sandbox worked around it with a temporary role and SELECT policies; that patch is now obsolete.

### Fixed — org membership read (2026-07-29)
- **Membership is read with the Supabase service role, not asyncpg.** New `adapters/platform/memberships.py` (`SupabaseMembershipReader`) queries `user_organization_memberships` (then `user_profiles`) over PostgREST with `SUPABASE_SERVICE_ROLE_KEY`, which bypasses the Hub's RLS — the same route image-to-cell-3 takes via `get_supabase_service()`. The `smartqr_*` tables stay behind asyncpg and RLS; the service role is used for this one read and nothing else.
- `SUPABASE_SERVICE_ROLE_KEY` is **mandatory outside `APP_ENV=local`**: without it the boot log says so at ERROR level and every org check fails closed. Wired into `.env.sandbox.example` and the staging task definition secrets.
- An inactive membership row is a denial (matches I2C's SH-183 rule). A failed read (bad key, 5xx after 3 retries, transport error) is *unknown*, which `require_auth` turns into an explicit 403 outside local — never a silent allow.
- The direct-SQL path remains only for a standalone local database with no platform tables; `OrgMembershipRepository(platform_reader=None)` selects it explicitly.
- Verified against the Supabase sandbox project: 16 memberships readable with the service role (asyncpg saw 0 under RLS), own org → allowed, foreign org → 403, unknown user → 403, invalid key → unknown (which fails closed rather than denying for the wrong reason).

### Security — pre-sandbox review
- **Browse endpoints require authentication.** `GET /workspaces`, `/items` and `/columns` read Smartsheet through the service-wide token and were open to anyone who could reach the port. New `require_user` dependency (no org header, so the shared Hub pickers keep working).
- **`X-Organization-ID` is verified against `user_organization_memberships`** (with `user_profiles` as fallback). Previously the header was trusted, so a valid session for org A could read and modify org B. Memberships are the source of truth because the Hub's active plan can differ from the profile's default org — checking only the profile would have rejected legitimate sessions. Cached 60s, **fails closed** outside `APP_ENV=local`.
- **`is_local` means local only.** It used to match `dev`/`development`/`test`, which would have left `POST /api/v1/dev/inject-event` reachable on a deployed task. That route now also requires auth and a matching org.
- **Webhook HMAC bound to its `webhookId`.** The old code fell back to any other cached secret when the id did not match, and skipped verification entirely without a JWT secret. Unknown secrets are now rejected outside local.
- **CORS allowlist** via `CORS_ORIGIN` (was `allow_origins=["*"]` with credentials).
- Migration `008_rls_lockdown.sql`: RLS on every `smartqr_*` table with no policies and no grants for `anon`/`authenticated`. On Supabase an ungoverned `public` table is readable with the anon key.

### Added
- **Request correlation:** `X-Request-Id` accepted from the Hub (or generated), echoed back, and prefixed on every log line; access log with method, path, status and duration. The Hub client sends one id per call.
- **Every `process_row` outcome is logged** with org, config, row, reason and duration. `paused`, `hash_unchanged`, `fill_if_empty`, `bolt_denied`, `watched_columns_unchanged` and `config_not_found` used to return silently.
- `scripts/sandbox_smoke.py` — re-runs the probe/auth/org-isolation cases of the sandbox plan (`--expect-local` for a developer machine).
- Managed-Postgres support: automatic TLS for non-local hosts (`DATABASE_SSL`), statement cache disabled behind a pgBouncer pooler (`DATABASE_STATEMENT_CACHE_SIZE`), and migrations applied under a Postgres advisory lock so replicas cannot race.
- `GET /ready` now checks Postgres and answers **503** when it is down (was Redis-only and always 200, so no orchestrator could act on it).
- `LOG_LEVEL` is configurable.
- Sandbox/staging deploy artifacts: `.env.sandbox.example`, `stg-smart-qr-env.example.json`, `ecs-task-definition.staging.template.json` (family `stg-smart-qr`, port 3021, log group `/ecs/smarthub-stg/smart-qr`).
- QR appearance controls (collapsible in Hub wizard when code type is QR): module/background colors, logo size 15–30%, quiet zone (border 1–4), box size S/M/L.
- Live preview via `POST /api/v1/qr-preview` (same renderer as Generate; optional logo upload or stored config logo).
- Migration `007_qr_style.sql` (`qr_fill_color`, `qr_back_color`, `qr_logo_scale`). Style changes clear row hashes so the next Generate rewrites images.

### Changed
- Generation is **on demand only**: no auto webhook on create; no auto backfill in the wizard. Use **Generate QRs** on Configs.
- QR error correction fixed to **H** (no user picker) to support center logos.
- Per-config **logo** upload (PNG/JPEG/WebP) stored on `smartqr_configs`; rendered in QR center when generating.
- Hub wizard + shared pickers support `variant="dark"` for readable SmartQR UI.

### Added (earlier)
- Content mode **Open row in Smartsheet** (`open_row`): QR encodes the row permalink from the Smartsheet API so shared users open that row on scan.
- `POST/DELETE /api/v1/configs/{id}/logo`
- Migration `003_logo_and_ecc_h.sql`

### Fixed
- Deleting the last config for a sheet unregisters its Smartsheet webhook and drops the cached secret (Smartsheet kept posting events for a config that no longer existed).
- `DELETE /api/v1/configs/{id}` no longer declares a response body for its 204 (failed to import under FastAPI 0.105).
- Identify mode no longer treats all-empty watched cells as payload `||` (generated barcodes and bolts on blank rows). Skip with no charge when content columns are empty.
- Webhook challenge now returns JSON `{"smartsheetHookResponse":...}` + `Smartsheet-Hook-Response` header (plain-text echo caused `DISABLED_VERIFICATION_FAILED` / Invalid JSON).
- Register webhook no longer deadlocks verification: Smartsheet SDK enable/create run in `asyncio.to_thread` so the same worker can answer the challenge within 5s (was `DISABLED_VERIFICATION_FAILED` / Request timed out).
- Register webhook deletes prior `smart-qr-*` webhooks for the sheet and fails loudly unless status is `ENABLED`.

### Added (earlier)
- Direct Smartsheet webhook ingress `POST /webhook/smartsheet/{org_id}` (challenge &lt;5s, HMAC, process events) for local/ngrok.
- `POST /api/v1/configs/{id}/register-webhook` + auto-register on create when `WEBHOOK_PUBLIC_BASE_URL` + token set.
- Gateway internal `PUT /internal/v1/webhook-secrets/{tool_slug}/{org_id}` + `smart-qr` stream map entry.
- Hub UI: Pause/Resume, Backfill sheet, Delete; Advanced ECC/write mode.
- Hub UI: shared Workspace/Worksheet/Column pickers + product shell aligned with Image-to-Cell (Back to Hub, header, tabs New/Configs/Information/How-to/Feedback/Usage/Connect).
- Fix cellimages upload to use raw body + Content-Disposition (I2C pattern); multipart caused Smartsheet 1011.
- Resolve content column titles → IDs on create when live Smartsheet token is set.
- NGROK.md local test guide.

### Added (scaffold)
- Initial SmartQR service scaffold (`backend/smart-qr/`): domain (payload, QR/Code128/EAN renderer, hash, write policy), Postgres migrations `smartqr_*`, REST `/api/v1`, Redis stream consumer, Hub module `frontend/src/products/SmartQR`, Docker Compose (Postgres/Redis/API).
- Lessons from I2C applied: Change-Agent filter, per-row cellimages lock + retries, bolt reserve/release (mock billing), hash skip.
