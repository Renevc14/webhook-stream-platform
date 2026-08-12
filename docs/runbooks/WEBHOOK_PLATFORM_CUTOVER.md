# Webhook platform cutover (SH-206)

How to move a tool from "receives Smartsheet callbacks itself" to "the shared
Webhook Gateway receives, verifies and publishes to a Redis stream".

Applies to **Image-to-Cell v3** and **SmartQR**. **Cell History 2 is out of
scope** — it keeps its own ingress and lifecycle until a dedicated ticket
covers it, and it must never be added to any gateway tool list.

---

## 1. Moving parts

```
Smartsheet
  └─ POST {base}/webhook/smartsheet/{tool_slug}/{org_id}
       └─ Gateway: challenge → HMAC verify (webhook-secrets:{org}:{provider}:{tool}) → XADD stream:{tool}
            └─ Tool worker (SmarthubWorker): process → ACK → DLQ on failure
Gateway lifecycle (singleton under lock:lifecycle:manager)
  ├─ secret sync   — pull encrypted secrets from each tool table into Redis (SH-173 / SH-208)
  └─ registrar     — register / renew / zombie-clean Smartsheet webhooks (SH-210)
```

| Tool | URL slug | Stream | Write SoT | Soft-fail mirror | Secret column (mirror) |
|---|---|---|---|---|---|
| Image-to-Cell v3 | `smart-image-to-cell` | `stream:i2c` | `platform_webhook_registrations` | `imagetocell_webhooks` | `shared_secret` |
| SmartQR | `smart-qr` | `stream:smart-qr` | `platform_webhook_registrations` | `smartqr_webhooks` | `shared_secret_encrypted` |

**SH-230 D write contract.** Every webhook mutate path (tool register/unregister, gateway registrar) writes **platform first (required)**. Tool `*_webhooks` tables are a soft-fail mirror so old readers and rollback stay possible until a follow-up drops them. Do **not** set `PLATFORM_WEBHOOK_READ=false` as a rollback for the staging 401 bug — heal or re-register so platform matches the live Smartsheet webhook instead.

Identity key for platform rows: `(provider, tool_slug, organization_id, worksheet_id)`.
Secrets are AES-GCM under the **same `APP_CRYPTO_KEY`** as every backend (never plaintext).
Redis holds plaintext only, with a 300s TTL, under
`webhook-secrets:{org}:{provider}:{tool}` (SH-215). The format is defined once in
`smarthub_core.webhooks.secret_cache` — do not hand-build it anywhere.

Three processes write that cache and they are all expected: the gateway's pull
sync, the gateway's registrar, and a tool pushing over
`PUT /internal/v1/webhook-secrets/{provider}/{tool}/{org}` right after
registering (Smartsheet can fire the first callback inside one sync cycle). A
**tool must never SET the key itself** — SmartQR used to, with a mismatched 24h
TTL, which is what SH-215 removed.

The pre-SH-215 key `webhook-secrets:{org}:{tool}` has no reader and no writer.
There is deliberately no fallback: it protected a window that does not exist
(I2C verifies HMAC against Postgres, never against this cache), and a key that
nothing may write but something still reads is a trap for the next person.
Leftovers expire on their own within one TTL; if a callback 401s in that window
it self-heals on the next sync cycle, which runs at the same 300s.

---

## 2. Flags

**Gateway** (`services/webhook-gateway`)

| Env | Default | Meaning |
|---|---|---|
| `RUN_LIFECYCLE_POLLER` | `true` | Run the locked lifecycle cycle at all |
| `SMARTQR_SECRET_SYNC_ENABLED` | `true` | Include `smartqr_webhooks` in secret sync |
| `RUN_WEBHOOK_REGISTRAR` | **`false`** | Let the gateway own register/renew/zombie |
| `WEBHOOK_REGISTRAR_TOOLS` | `smart-image-to-cell` | Comma list of slugs the registrar manages |
| `WEBHOOK_REGISTRAR_MIN_AGE_MINUTES` | `10` | Grace period before recreating a `NEW_NOT_VERIFIED` webhook |
| `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL` | — | Required by the registrar; e.g. `https://webhooks.prodactivelabs.com` |
| `PLATFORM_WEBHOOK_DUAL_WRITE` | `true` | Soft-fail mirror into per-tool `*_webhooks` after required platform write (SH-230 D). Platform remains write SoT. |
| `PLATFORM_WEBHOOK_READ` | **`false`** | Secret sync reads platform table instead of per-tool tables (SH-230 C). Staging enables after backfill. **Do not flip false to “fix” stale platform rows** — heal/re-register instead. |
| `ENCRYPT_TOOL_INTERNAL_URL` | — | encrypt-tool internal base for OAuth broker (required for OAuth-only orgs when registrar runs) |
| `SMARTSHEET_BROKER_SECRET` | — | m2m secret for `/internal/token/resolve` (pair with encrypt-tool) |
| `GATEWAY_M2M_TOKEN` | — | Bearer for `/internal/v1/*` (secret push + ensure-registration). Required outside local |

**Tools**

| Env | Tool | Meaning |
|---|---|---|
| `RUN_WEBHOOK_RECEIVER` | I2C | Mount the tool's own `/webhook/smartsheet` route. Set `false` after cutover; the Hub API, health and stream consumer stay up. **SmartQR has no such route and no such flag** |
| `RUN_STREAM_CONSUMER` | I2C, SmartQR | Run the stream worker |
| `RUN_LIFECYCLE_POLLER` | I2C | Tool-side register/zombie. Must be `false` before the gateway registrar takes the tool |
| `WEBHOOK_LIFECYCLE_ENABLED` | I2C | With `RUN_LIFECYCLE_POLLER`, controls `tool_owns_webhook_lifecycle`. Both false → Pulse path (ensure-registration) |
| `GATEWAY_INTERNAL_URL` | I2C (cutover) | Base URL for m2m ensure / secret push (e.g. `https://webhooks.prodactivelabs.com`) |
| `GATEWAY_M2M_TOKEN` | I2C (cutover) | Same shared token as gateway |
| `SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL` | I2C, SmartQR | Gateway public base for callback URL construction |

> **Single-owner rule.** Exactly one of {tool lifecycle poller, gateway
> registrar} may manage a given tool's webhooks. With both on they delete each
> other's registrations: the tool's cleanup treats the gateway URL as foreign
> and vice versa.

---

## 3. Cutover order

Per tool, per environment. Do not batch.

1. **Prove secret sync.** Gateway with `RUN_LIFECYCLE_POLLER=true`, registrar
   off. Confirm the cache fills for the orgs you expect:

   ```bash
   redis-cli --scan --pattern 'webhook-secrets:*'
   redis-cli get "webhook-secrets:<org>:smartsheet:<tool>"   # {"<webhook_id>":"<secret>"}
   ```

   A 3-segment key without the provider is a pre-SH-215 leftover. Nothing reads
   it — it means the sync has not written that org yet. Let it expire.

   Cycle logs read `webhook_gateway: lifecycle cycle done {...}`.

2. **Start the consumer.** Deploy the tool with `RUN_STREAM_CONSUMER=true`
   while its direct webhook route is still mounted. Nothing arrives on the
   stream yet — you are only proving the worker boots and joins its group.

   ```bash
   redis-cli xinfo groups stream:smart-qr
   ```

3. **Register against the gateway (single owner).**

   - Set the tool's `RUN_LIFECYCLE_POLLER=false` (and I2C
     `WEBHOOK_LIFECYCLE_ENABLED=false`) and redeploy it.
   - Gateway: `RUN_WEBHOOK_REGISTRAR=true`, slug in `WEBHOOK_REGISTRAR_TOOLS`,
     `GATEWAY_M2M_TOKEN` set, broker env for OAuth-only orgs.
   - **I2C create / `register-webhook`:** with lifecycle off, I2C calls
     `POST /internal/v1/lifecycle/ensure-registration` (does **not** call
     Smartsheet itself). Expect gateway log
     `ensure_registration done … ok: True` and HTTP 200.
   - **SmartQR (today):** still registers from the tool; must push secrets
     via m2m. Follow-up: add `smart-qr` to `WEBHOOK_REGISTRAR_TOOLS` and
     turn off its lifecycle (same pattern as I2C).

   Periodic cycles still report `registered` / `url_updated` / `healthy`
   under the `registrar` key of the lifecycle summary.

4. **Verify a real callback.** Touch a watched cell and follow it:

   ```bash
   # gateway accepted + published
   #   webhook_gateway: accepted provider=smartsheet tool=... org=... msg=...
   redis-cli xlen stream:smart-qr
   redis-cli xlen stream:smart-qr:dlq        # must stay 0
   redis-cli xpending stream:smart-qr smart-qr-workers
   ```

5. **Close the direct route.** `RUN_WEBHOOK_RECEIVER=false` on I2C. Configs,
   health and the stream consumer stay up. SmartQR has no step here: it never
   ships a receiver, so the gateway is its only ingress from day one.

6. **Sweep zombies.** With the registrar on, webhooks whose config was deleted
   or paused are removed automatically and logged as
   `registrar deleting zombie webhook=...`.

---

## 4. Safety properties to keep in mind

- **Ownership by URL prefix.** The registrar only touches webhooks whose
  `callbackUrl` contains `{base}/webhook/smartsheet/{tool_slug}/`. Another
  tool's webhook on the same sheet — including CH-2 — is invisible to it.
- **Cross-org protection.** Webhook ids registered to a different
  `organization_id` are never deleted and never counted as duplicates.
- **Fail closed.** If the ownership lookup errors, that cycle deletes nothing
  and skips the destructive pre-cleanup on register. It retries next cycle.
- **Secret capture is one-shot.** Smartsheet returns `sharedSecret` only on
  create/enable. The registrar encrypts it into the tool table and merges it
  into the Redis map immediately; a webhook registered outside these paths
  cannot be verified later.
- **One owner per cache key.** Only the gateway writes
  `webhook-secrets:{org}:{provider}:{tool}`, through `WebhookSecretStore` or the
  internal m2m route. A tool writing it directly reintroduces the SH-215 bug:
  divergent TTLs, so a revoked secret stays verifiable after the gateway's copy
  expires.
- **At-least-once delivery.** A 5xx from the gateway makes Smartsheet retry.
  Workers must be idempotent: I2C dedups per envelope and per attachment,
  SmartQR dedups by row payload hash.

---

## 5. Rollback

Rollback is per step and needs no data migration.

| Symptom | Action |
|---|---|
| Registrar misbehaving | `RUN_WEBHOOK_REGISTRAR=false`, re-enable the tool's `RUN_LIFECYCLE_POLLER` |
| Callbacks rejected 401 | Check `webhook-secrets:{org}:smartsheet:{tool}` exists; re-register the sheet to capture a fresh secret |
| Stream backing up | Check `xpending` and `stream:{tool}:dlq`; scale the consumer or fix the failure the DLQ reason names |
| Need the direct route back (I2C only) | `RUN_WEBHOOK_RECEIVER=true` and point the webhook URL back at it. Not an option for SmartQR: fix the gateway or pause the configs |

---

## 6. Where things live

| Area | Path |
|---|---|
| Gateway ingress | `services/webhook-gateway/src/gateway/routes/webhooks.py` |
| Secret sync | `services/webhook-gateway/src/gateway/lifecycle/secret_sync.py` |
| Secret cache key contract | `shared/smarthub_core/src/smarthub_core/webhooks/secret_cache.py` |
| Secret store (gateway) | `services/webhook-gateway/src/gateway/secrets/store.py` |
| Registrar (SH-210) | `services/webhook-gateway/src/gateway/lifecycle/registrar.py` |
| Shared webhook CRUD | `shared/smarthub_core/src/smarthub_core/platforms/smartsheet/webhook_service.py` |
| Platform registration SoT helpers | `shared/smarthub_core/src/smarthub_core/webhooks/platform_registrations.py` |
| Ensure-registration (m2m) | `services/webhook-gateway/src/gateway/routes/internal.py` + `lifecycle/manager.py` |
| I2C gateway client (ensure / secret push) | `backend/image-to-cell-3/src/adapters/gateway/client.py` |
| Callback URL contract | `shared/smarthub_core/src/smarthub_core/platforms/smartsheet/callback_urls.py` |
| Worker base | `shared/smarthub_core/src/smarthub_core/worker/base.py` |
| I2C worker | `backend/image-to-cell-3/src/workers/stream_worker.py` |
| SmartQR worker | `backend/smart-qr/src/smart_qr/workers/stream_worker.py` |
| TextNow gap analysis | [`docs/architecture/TEXTNOW_WEBHOOK_MIGRATION.md`](../architecture/TEXTNOW_WEBHOOK_MIGRATION.md) |

---

## 7. Staging big-bang checklist (SH-230 C + I2C cutover)

Goal: staging mirrors the intended pre-prod shape so Pulse can be smoke-tested end-to-end before the next production cut.

### Pre-deploy (Supabase staging)

1. Apply `20260731120000_platform_webhook_registrations.sql` if not already.
2. Apply `20260802120000_platform_webhook_registrations_backfill.sql` (I2C + SmartQR → platform).
3. Spot-check parity (optional SQL):

   ```sql
   -- only_tool should be ~0 for active rows after backfill
   SELECT 'i2c' AS tool,
     (SELECT count(*) FROM imagetocell_webhooks WHERE status = 'active') AS tool_n,
     (SELECT count(*) FROM platform_webhook_registrations
        WHERE tool_slug = 'smart-image-to-cell' AND status = 'active') AS platform_n;
   ```

### Deploy order (ECS)

1. **I2C first** with lifecycle + direct receiver **off** (template already pins this):
   - `RUN_LIFECYCLE_POLLER=false`
   - `WEBHOOK_LIFECYCLE_ENABLED=false`
   - `RUN_WEBHOOK_RECEIVER=false`
   - `RUN_STREAM_CONSUMER=true`
2. **Webhook gateway** with registrar + platform reads **on**:
   - `RUN_LIFECYCLE_POLLER=true`
   - `RUN_WEBHOOK_REGISTRAR=true`
   - `WEBHOOK_REGISTRAR_TOOLS=smart-image-to-cell`
   - `PLATFORM_WEBHOOK_DUAL_WRITE=true`
   - `PLATFORM_WEBHOOK_READ=true`
3. Confirm gateway logs: `secret sync sources=... platform_read=True` and registrar cycle with `registered`/`healthy`.
4. Confirm Redis: `webhook-secrets:<org>:smartsheet:smart-image-to-cell` and `:smart-qr`.
5. Touch a watched I2C sheet → gateway `accepted` → `stream:i2c` → consumer ACK.
6. Confirm no second owner: I2C boot log must show lifecycle/receiver skipped.

### Rollback staging

- Gateway: `RUN_WEBHOOK_REGISTRAR=false` (keep `PLATFORM_WEBHOOK_READ=true` if secret sync already depends on platform; heal stale rows instead of flipping read off)
- I2C: `RUN_LIFECYCLE_POLLER=true`, `WEBHOOK_LIFECYCLE_ENABLED=true`, `RUN_WEBHOOK_RECEIVER=true`

Do **not** enable gateway registrar for I2C while I2C lifecycle is still true.

### Staging heal — stale platform vs tool table (SH-230 D)

If gateway returns 401 with `PLATFORM_WEBHOOK_READ=true` while the tool table has a newer `webhook_id` / secret, copy tool → platform for that identity (staging only):

```sql
-- Example: org 78e2278d… / sheet 1124052763627396
UPDATE platform_webhook_registrations AS p
SET
  webhook_id = t.webhook_id::bigint,
  shared_secret = t.shared_secret,
  callback_url = COALESCE(t.webhook_url, p.callback_url),
  status = t.status,
  updated_at = now()
FROM imagetocell_webhooks AS t
WHERE p.provider = 'smartsheet'
  AND p.tool_slug = 'smart-image-to-cell'
  AND p.organization_id = t.organization_id
  AND p.worksheet_id = t.worksheet_id
  AND t.organization_id = '78e2278d-ffaa-4299-a969-9424275cc207'::uuid
  AND t.worksheet_id = 1124052763627396;
```

Then invalidate Redis `webhook-secrets:<org>:smartsheet:smart-image-to-cell` (or wait ~300s TTL) so secret sync reloads ciphertext from platform.

Do **not** run this against production unless explicitly requested.

---

## 8. Staging → prod promote (after smoke)

Single-owner lifecycle already required. Order after staging smoke is green
(PR [#206](https://github.com/ProdactiveSmartHub/smarthub-main/pull/206) tip
`69f6605` / charge-on-process `d824e30` / staging task **`:26`** carries I2C
BillingPort + gateway cutover + SH-108/estimate/last_ts + create single-config;
SmartQR SH-233 merges separately). Same SHA in the PR is the SHA to build into
prod ECR. After #206 merges to `develop`, follow the release train to `main`.

### 8.1 Progress (2026-08-04)

| Step | Status |
|---|---|
| Supabase prod `platform_webhook_registrations` DDL | ✅ applied (`ouchuovqcxkltxgwjwdw`) |
| Supabase prod I2C → platform backfill | ✅ applied (SmartQR backfill deferred — no `smartqr_webhooks`) |
| Supabase prod SH-234 usage_log reprocess indexes | ✅ applied |
| Hub billing `20260730140000` (`get_org_billing_usage_total`) | ❌ deferred |
| Prod ElastiCache `smarthub-redis-production` | ✅ available (AUTH+TLS, `rediss://`) |
| ECR `prod-webhook-gateway` + images `:534f0d7` (gateway + I2C) | ✅ pushed |
| Secrets `REDIS_URL` / `GATEWAY_*` / `prod/webhook-gateway-env` | ✅ wired (shared M2M; `APP_CRYPTO_KEY` unchanged) |
| ALB TG + DNS `webhooks.getsmarthub.co` + ECS cluster | ✅ rule prio 11 + Route53; TG targets healthy |
| `iam:PassRole` | ✅ verified (register TD succeeded after IAM grant) |
| I2C ECS cutover | ✅ **`prod-image-to-cell:11`** HEALTHY — lifecycle/receiver OFF, stream ON, image `534f0d7` |
| Gateway ECS service | ✅ **`prod-webhook-gateway:1`** HEALTHY — registrar ON for `smart-image-to-cell` only, `PLATFORM_WEBHOOK_READ=true` |
| Edge health | ✅ `https://webhooks.getsmarthub.co/health` 200; I2C `/health` 200 |
| SmartQR prod | ❌ not provisioned (no `prod-smart-qr` ECR/ECS) |
| Product smoke (create/ensure + live attachment + SH-234 reprocess) | ⏳ still owed |

**Cutover order used:** merge REDIS/GATEWAY keys into `prod/image-to-cell-env` → register I2C cutover TD → force deploy → HEALTHY boot confirm → register gateway TD + ALB host rule + DNS → create gateway service with registrar ON. No dual owners.

### 8.2 Remaining (post-cutover)

1. **Product smoke:** safe-org create/ensure + attachment → gateway `accepted` → `stream:i2c` → cell upload; clear cell + Run Backfill → charge-on-process (SH-234).
2. **Jira:** note prod task revs on SH-230 / SH-234.
3. **SmartQR:** first prod provision when ready (tool-owned lifecycle — **not** on `WEBHOOK_REGISTRAR_TOOLS`).
4. Scrub any local `.deploy-tmp` files that held secret JSON.

Do **not** flip `PLATFORM_WEBHOOK_READ=false` as a 401 fix; prefer heal platform rows / registrar. Do **not** leave registrar + I2C lifecycle both true.

**Billing note (I2C + SmartQR):** mid-job metering uses `BillingPort` /
`BoltGate` with fail-closed Redis/Hub outside local. **I2C product policy
(2026-08-03):** charge on every successful process (including reprocess after
clear); skips free; SH-111 shuffle free; no Redis refund on `ALREADY_LOGGED`
after a successful write. Hold TTL is metadata only — a crash leaves Redis
high until cycle reseed from `get_org_billing_usage_total` (safe side = false
deny). No background hold reconciler in this slice.

**Staging progress (2026-08-03):**
- Cutover smoke: ensure on config create OK; attachments billed; gateway
  `accepted` → `stream:i2c` → cell upload.
- Task ladder `:22`→`:23` (refund-dup) →`:24` (create single-config + SH-108
  estimate) →`:25` (last_ts) → **`:26`** (`d824e30`, charge-on-process).
- Post-`:26` org `78e2278d-…` / sheet `1124052763627396`: Redis =
  usage_log×15; **0** duplicate refunds; estimate matched processable.
- Create single-config path **not** re-smoked under `:26`.
- **Prod Pulse cutover applied 2026-08-04** — I2C `:11` + gateway `:1` (`534f0d7`); product smoke still owed.
