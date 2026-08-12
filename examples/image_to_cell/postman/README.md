# i2c-3 — Postman collection

Postman collection covering every public HTTP endpoint of the Image-to-Cell v3
backend. Includes a Supabase login request that auto-extracts the JWT so every
other request inherits the auth headers without manual copy/paste.

## Import

In Postman: **File → Import → Upload Files** and pick
[`i2c-3.postman_collection.json`](./i2c-3.postman_collection.json).

## Setup (one-time)

Open the collection's **Variables** tab and fill these:

| Variable | What it is | Where to get it |
|---|---|---|
| `email` | Your staging Supabase user email | The account you log into the staging app with |
| `password` | Same user's password | idem |
| `org_id` | Your organization UUID | Logged-in app: settings page / DB / browser devtools |

The rest is pre-filled (`supabase_url`, `supabase_anon_key`, `i2c_url`, the
local `worksheet_id` and `config_id` we used while developing SH-121).

## Daily flow

1. **00 / Login** — run once. The test script automatically stores
   `access_token`, `refresh_token`, and `user_id` as collection variables. The
   token is valid for 1 hour.
2. Run any other request in the collection. The Bearer token is injected at the
   collection level, the `X-User-ID` header reads from `{{user_id}}`, and the
   `X-Organization-ID` from `{{org_id}}`.
3. When the token expires, run **00 / Refresh token** instead of re-typing the
   password.

## Folders

| Folder | Contents |
|---|---|
| **00 — Auth** | Supabase login + refresh. Run login first. |
| **01 — Health & Info** | `/health`, `/ready`, `/info`, `/metrics`. Public endpoints, no auth. |
| **02 — Smartsheet Discovery** | List workspaces / worksheets / columns. Used by the frontend to populate pickers. |
| **03 — Configurations** | CRUD on configs + register-webhook + stats. |
| **04 — Backfill** | The new **SH-121** `POST /configs/backfill/estimate` (read-only preview) and the existing `POST /configs/backfill` (enqueues the job). |
| **05 — DLQ** | Stats, list, retry, resolve events from the dead-letter queue. |
| **06 — Webhooks** | `GET /webhook/status` (the Smartsheet-callback receiver is in here too but you can't hit it manually without a signed body). |
| **07 — Auth tests (negative)** | Three requests that are EXPECTED to fail (401 / 403). Verify the JWT gate is enforcing once `SUPABASE_JWT_SECRET` is set in `.env`. |
| **99 — Danger zone** | Destructive operations (`DELETE` config, `DELETE /dlq/cleanup`). Don't run unless you mean it. |

## Auto-tests included

- **Login**: validates the response shape and stores tokens as variables.
- **Backfill Estimate (SH-121)**: validates the response has the right keys,
  that `bolts_needed == images_needed × bolts_per_image`, and that
  `bolts_overage_estimated == max(0, bolts_needed - bolts_available_in_cap)`.
- **Auth tests folder**: each request asserts the expected failure status (401
  or 403) so you can spot it the moment the JWT gate stops enforcing.

Run a folder with **"Run folder"** in Postman to execute every request in
sequence with its tests; check the Test Results tab for a pass/fail summary.

## Pointing at staging or production

By default `i2c_url` is `http://127.0.0.1:3011` (your local uvicorn). To hit
deployed environments, edit the `i2c_url` variable:

- Staging: `https://smart-image-to-cell-staging.prodactivelabs.com` (confirm
  with infra)
- Production: `https://smart-image-to-cell.prodactivelabs.com`

The Supabase project and anon key are shared across environments — only the
backend URL changes.

## What's NOT in the collection

- `POST /webhook/smartsheet` — receives webhooks from Smartsheet with an HMAC
  signature. You can't reasonably fake the signature, so testing it manually
  doesn't add value; the unit tests cover its logic.
- `POST /manual-trigger` — requires a real Smartsheet event payload to be
  meaningful.

Those are wired in the codebase, just not exposed here as click-to-run.
