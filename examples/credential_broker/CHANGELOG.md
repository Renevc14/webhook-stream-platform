# Changelog — Encryption Tool

Per the Smarthub Versioning Strategy v1 ([`docs/versioning/README.md`](../../docs/versioning/README.md)).

Tool slug: `encryption-tool` · Tracked in `system_tools` (Supabase).
Folder name (`encrypt-tool`) is the historical service name.

---

## [Unreleased]

**Category:** Backend / Security · **Impact:** PATCH (→ **1.1.3**, SH-226 Cell History READ_USERS; staged alongside platform unreleased)

### Deployed

- **Prod (2026-08-04):** ECS **`prod-encrypt-tool:5`** · ECR **`prod-encrypt-tool:487f449`** (+ `:latest`) · cluster `prod-encrypt-tool-cluster` / service `prod-encrypt-tool-service` — HEALTHY. Carries `TOOL_SCOPES["smart-qr"]` write+webhook scopes (see Fixed below). No `APP_CRYPTO_KEY` rotation.

### Fixed

- **SH-226 — `single-cell-audit` lacked `READ_USERS`.** CH-2 resolves webhook `userId` via `GET /users/{id}` for Changed By attribution; without `READ_USERS` Smartsheet returns 403 and the audit trail falls back to `User <id>`. `TOOL_SCOPES` now includes `READ_USERS` alongside the SH-201 write/admin/webhook set. Existing tokens (including post-SH-201) must re-consent — refresh cannot widen scopes; `/oauth/status` already surfaces `needs_reconnect` / `missing_scopes`.
- **Boot-time assert refuses to start when a live tool is missing from `TOOL_SCOPES`.** `REQUIRED_TOOL_SLUGS` is an independent set (not derived from the table), and `assert_tool_scopes_complete()` runs at import. Deleting an entry — the failure mode that under-scoped Cell History (SH-201) and SmartQR — now crashes the credentials service instead of silently granting `DEFAULT_SCOPES = ["READ_SHEETS"]`. Unknown slugs still fall back to the default but log at ERROR so a new tool cannot ship under-scoped unnoticed.
- **`smart-qr` was missing from `TOOL_SCOPES`, so it consented read-only.** Unregistered slugs fall back to `DEFAULT_SCOPES = ["READ_SHEETS"]`; SmartQR therefore minted tokens that could browse sheets but got `403 errorCode 1004` on every `cellimages` write. It now requests `READ_SHEETS`, `WRITE_SHEETS`, `ADMIN_WEBHOOKS` (same shape as Image-to-Cell). Tokens issued before this fix must re-consent — refresh cannot widen scopes — and `/oauth/status` reports them via `missing_scopes`.
- **SH-201 — `single-cell-audit` OAuth scopes were read-only.** `TOOL_SCOPES` now grants `READ_SHEETS`, `WRITE_SHEETS`, `ADMIN_SHEETS`, and `ADMIN_WEBHOOKS` so Cell History can create/lock CH columns, write cells, and manage webhooks (parity with Smartsheet’s scope map + Image-to-Cell’s webhook needs). Existing under-scoped tokens cannot gain scopes via refresh.
- **SH-201 — `/oauth/status` now flags scope drift.** `needs_reconnect` is true when the stored token’s scopes are a proper subset of `scopes_for_tool(slug)`; response also includes `missing_scopes` so the Connect UI prompts re-consent after a scope expansion. Helpers: `missing_scopes_for_tool`, `_tools_status_payload`.

### Changed

- **SH-182 — Explicit OAuth runner state on the internal token broker (`POST /internal/token/resolve`).** Every resolve response now carries `oauth_state` (`active` / `revoked` / `error` / `missing` / `unknown`), plus `fallback_used` and `fallback_reason` when a transition org PAT is returned. Resolution matrix: healthy OAuth → 200 `source=oauth`, `oauth_state=active`; revoked/terminal/missing OAuth + PAT → 200 `source=pat` with explicit fallback reason; revoked/terminal **without** PAT → typed **409** (`CONNECTION_REVOKED`, `TOKEN_REFRESH_FAILED`, etc.) — never collapsed into `NO_CREDENTIALS`; no OAuth + no PAT → existing 404; transient refresh failure without PAT → 503 `OAUTH_TEMPORARILY_UNAVAILABLE`. Mid-request refresh that turns terminal (e.g. Smartsheet 1004) is re-read and classified terminal, not transient. Audit meta records `oauth_state` / `fallback_reason`; plaintext tokens never appear in audit output.
- **Shared `smarthub_core` resolver (same release train).** `SmartsheetTokenResolver._broker_resolve` propagates safe broker metadata into `ResolvedToken.metadata` and raises `TokenRevokedError` (subclass of `TokenNotFoundError`) on broker 409 so SH-178 runner-failover callers can branch without guessing from a generic no-credentials error. Broker 5xx/unreachable still falls back to legacy tables; plaintext is never written to Redis. Tests: 12 new matrix cases in `backend/encrypt-tool/tests/test_oauth_smartsheet.py`; 9 broker-contract tests in `shared/smarthub_core/tests/platforms/test_smartsheet_token_resolver.py`.

---

**Category:** Backend / Security · **Impact:** MINOR (→ **1.1.0**, staged for platform `1.7.0` — Smartsheet Connect, SH-160)

### Added

- **Smartsheet OAuth core (`oauth_smartsheet.py`)** — the credentials service is now the single home for per-user, per-tool Smartsheet OAuth tokens:
  - `POST /oauth/authorize` — builds the region-correct Smartsheet authorize URL (US live; EU supported in `OAUTH_REGIONS`, pending app registration) with least-privilege **per-tool scopes** (`TOOL_SCOPES`).
  - `GET /oauth/callback` handling — exchanges the code, verifies the user, and stores one scoped token **per tool** so each tool has its own Smartsheet rate-limit bucket.
  - `GET /oauth/status` — per-org connection + per-tool token status consumed by the frontend Connect page and tool Connect buttons.
  - **Internal token broker** — tool backends exchange a shared secret for a short-lived plaintext access token + regional base URL; the broker transparently refreshes expiring tokens (lease-based, race-safe) and falls back to the org PAT server-side, so GOV/AUS/EU API-key plans work through the same front door.
- **Tests** — `tests/test_oauth_smartsheet.py` (23 passing), including the disconnect-scope guard cases.

### Security

- **Token vault:** access + refresh tokens encrypted at rest with **AES-256-GCM** and AAD context binding (org/user/tool), decrypted only inside this service.
- **Revoke-scope guard:** `POST /oauth/disconnect` requires either a `product_slug` (single-tool revoke) or an explicit `disconnect_all: true`; ambiguous requests get `400 DISCONNECT_SCOPE_REQUIRED`. Fixes an incident where a single-tool revoke dropped every tool's connection.
- **CSRF-safe OAuth state:** single-use hashed state nonces with a 10-minute TTL and a per-user pending-state cap.
- **Audit trail:** connect, refresh, revoke, and disconnect events logged via `audit_log` with status detail (no token material in logs).
- **OWASP audit remediation (post-review hardening):**
  - Broker route returns **404 on the public `/encrypt` path** — plaintext-token resolution only exists on the internal root registration (H-1).
  - `token_resolve` now **verifies org membership** before releasing a per-user OAuth token — spoofed user ids from legacy header-auth tool backends can no longer pull another user's token; PAT fallback (org-scoped) unaffected (M-1).
  - **Successful resolves are audited** (`resolve`/`success` with source oauth|pat) so "which service pulled whose token, when" is answerable (M-2).
  - **Rate limiting:** per-caller sliding window on `/oauth/*` (30/min), per-IP lockout after 5 bad broker secrets in 10 min (`BROKER_AUTH_LOCKED`), and the pending-state quota check now **fails closed** (503) instead of silently bypassing (M-3).
  - **Open-redirect guard:** `return_path` must be a same-site relative path or it is dropped (L-5).
  - 4 new regression tests (27 total passing).

---

## [1.0.0] - 2026-04-27

**Category:** Security · **Impact:** MAJOR

- Initial 1.0.0 baseline for this tool. From here forward each release that touches the Encryption Tool appends a new section above and bumps the version per impact level. Pay extra attention to the **Security** subsection — most changes here are security-relevant.
