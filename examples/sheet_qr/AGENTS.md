# smart-qr — QR / barcode into cells (FastAPI)

See root `CLAUDE.md` and @docs/tools/TOOL_REGISTRY.md.

## Run commands

- Compose (from repo root): `docker compose -f backend/smart-qr/docker-compose.yml up -d --build`
- Tests: `cd backend/smart-qr && PYTHONPATH=src pytest tests/unit -q`
- Dev port: **3021** · slug `smart-qr` · Change-Agent `smart-qr-v1`

## Mode

On-demand generation (**Generate codes** in the Hub) plus a direct Smartsheet
webhook and a `stream:smart-qr` consumer. Nothing is generated automatically on
config create.

## Tables (owns)

`smartqr_configs`, `smartqr_webhooks`, `smartqr_row_states`, `smartqr_usage_log`,
`smartqr_schema_migrations`. Migrations live in `backend/smart-qr/migrations/`
and are applied at boot under an advisory lock. The only Hub tables it reads are
`user_organization_memberships` and `user_profiles`, and only to confirm org
membership — over the Supabase REST API with the service role key, never over
asyncpg (their RLS policies are written for browser sessions, so our Postgres
role sees zero rows).

## Rules specific to this service

- `APP_ENV=local` is the **only** environment that relaxes auth. Never set
  `local` on a deployed task, and never widen `Settings.is_local`.
- Org comes from `X-Organization-ID` but is verified against the Hub's
  memberships (active plan) with `user_profiles` as fallback. Outside local the
  check fails closed — do not add an "allow on error" path.
- `SUPABASE_SERVICE_ROLE_KEY` is mandatory outside local: without it the
  membership read returns nothing and every request is 403. It is used **only**
  for that read; the `smartqr_*` tables always go through asyncpg + RLS.
- Any new table must be added to the RLS lockdown migration; on Supabase an
  ungoverned `public` table is readable with the anon key.
- Webhook signatures are bound to their `webhookId`. Never fall back to another
  cached secret.
- Style/logo changes must clear `smartqr_row_states` hashes, otherwise the next
  Generate skips rows that need rewriting.
- Billing is `MockBilling` only; nothing is charged yet. Do not present bolt
  numbers as authoritative.

## Known gaps (pre-staging)

No `/metrics`, no per-row error persistence (`last_errors` is unused), no DLQ or
stream dedup, no rate limiting, single service-wide Smartsheet token. Tracked in
@docs/development/SMARTQR_DEV_PLAN.md.
