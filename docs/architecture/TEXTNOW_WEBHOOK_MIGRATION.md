# TextNow — webhook migration onto Gateway + streams (SH-214)

**Status:** tracking only. No TextNow code was changed by SH-206; this document
records the gap analysis so the rewrite can be scheduled as its own ticket.

**Parent:** SH-206 (centralized webhooks) · **Related:** SH-210 (gateway
lifecycle), SH-208 (multi-tool secret sync), SH-212 (worker base).

---

## 1. Where TextNow stands today

`backend/textnow/` is six flat modules (~2.3k LOC), no package layout, no test
suite. It is the least migrated of the active tools.

| Concern | I2C / SmartQR (post SH-206) | TextNow today |
|---|---|---|
| Ingress | Gateway verifies HMAC, publishes to `stream:{tool}` | `POST /webhooks/smartsheet` handled in-process (`app.py:953`) |
| Signature check | HMAC SHA-256 against cached shared secret | **none** — payload is trusted |
| Shared secret | encrypted column + `webhook-secrets:{org}:{tool}` cache | **never captured** from the create response |
| Callback URL | `{base}/webhook/smartsheet/{tool}/{org}` | `{WEBHOOK_BASE_URL}/webhooks/smartsheet` — one global URL, no org, no tool |
| Processing | stream consumer on `SmarthubWorker` (retry + DLQ) | full sheet fetch + SMS send **inside** the HTTP handler (`event_processor.py:90-132`) |
| Shared library | `smarthub_core` (crypto, redis, supabase, Smartsheet client) | private `crypto_helper.py`, two ad-hoc Smartsheet clients, no retry anywhere |
| Runtime roles | `RUN_HTTP_API` / `RUN_STREAM_CONSUMER` / `RUN_LIFECYCLE_POLLER` | no settings module at all — bare `os.getenv` |
| Redis | streams, dedup, rate limit, locks | none; caches are process-local dicts (`app.py:56-58`) |

### Blocking findings

1. **No HMAC and no stored secret.** `webhook_manager.ensure_webhook_registered`
   (`webhook_manager.py:13`) reads only `id` from the create response, so the
   `sharedSecret` Smartsheet returns exactly once is discarded. The gateway
   cannot verify TextNow callbacks until that secret is captured and stored.
2. **`smartsheet_webhooks` has no `organization_id`.** Every lookup keys on
   `worksheet_id` alone (`webhook_manager.py:41,192,253`). Per-org callback
   URLs and the SH-210 cross-org ownership gate both need that column. The
   table name is also unprefixed, unlike `imagetocell_webhooks` /
   `smartqr_webhooks`.
3. **The handler does the work synchronously**, including `Sheets.get_sheet` on
   the whole sheet and the Telnyx send. This breaks the <5s webhook rule
   (CLAUDE.md rule 9) and means a Telnyx blip loses the event: failures are
   swallowed and answered with 200 (`app.py:1014-1020`).
4. **No retry on any external call** (CLAUDE.md rule 2) and no
   `Smartsheet-Change-Agent` header on writes (rule 11).

---

## 2. Target shape

Identical to SmartQR after SH-209/SH-212:

```
Smartsheet → Gateway /webhook/smartsheet/textnow/{org}
           → HMAC verify (webhook-secrets:{org}:textnow)
           → XADD stream:textnow
           → TextNowStreamWorker(SmarthubWorker) → process_row_event → Telnyx
```

The gateway side is already generic: adding TextNow is a `WebhookToolSpec` in
`services/webhook-gateway/src/gateway/lifecycle/registrar.py`, a
`WebhookSecretSource` in `secret_sync.py`, and `"textnow"` in
`TOOL_STREAM_MAP`. None of that is useful until the TextNow schema and
settings exist, which is why this ticket is tracking-only.

---

## 3. Proposed ticket breakdown

| # | Work | Depends on |
|---|---|---|
| 1 | Migration: add `organization_id`, `shared_secret_encrypted`, `status` to `smartsheet_webhooks` (or a new `textnow_webhooks` table + backfill). Smartsheet ids stay BIGINT. | — |
| 2 | Introduce a real settings module (pydantic `BaseSettings`) with `RUN_HTTP_API` / `RUN_WEBHOOK_RECEIVER` / `RUN_STREAM_CONSUMER`, mirroring I2C SH-151/SH-211. | — |
| 3 | Replace `crypto_helper.py` with `smarthub_core.crypto`, and both Smartsheet clients with `SmartsheetClient` + `SmartsheetWebhookService` (brings retry, rate limit, Change-Agent). | 2 |
| 4 | Capture `sharedSecret` on register, encrypt with the shared `APP_CRYPTO_KEY`, store per org. | 1, 3 |
| 5 | `TextNowStreamWorker(SmarthubWorker)` on `stream:textnow`; move `process_row_event` behind it; keep the direct route until cutover. | 2, 3 |
| 6 | Gateway: add the TextNow secret source + registrar spec + stream mapping; register real webhooks with the 3-segment URL. | 1, 4, 5 |
| 7 | Cutover per org: `RUN_WEBHOOK_RECEIVER=false`, re-register webhooks, delete the legacy global-URL webhooks. | 6 |

Steps 1–5 are TextNow-local and can land while the gateway stays untouched.
Only step 6 touches shared gateway code.

---

## 4. Risks

- **Re-registration is mandatory.** The current webhooks point at a URL with no
  org segment, so they cannot be migrated in place — each sheet must be
  re-registered and the old webhook deleted, which is a visible per-org
  operation.
- **Secret capture is one-shot.** Smartsheet returns `sharedSecret` only on
  create/update. Any webhook not re-registered stays unverifiable by the
  gateway; there is no way to read the secret back later.
- **No tests exist**, so the worker rewrite must ship with its own unit suite
  (the SH-212 SmartQR tests are the closest template).
- **Cell History 2 is out of scope** for this and every other SH-206 ticket.
