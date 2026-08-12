# Smarthub — Agent Onboarding

> For: AI agents (Cursor's agent, Claude Code, future LLMs).
> Last updated: 2026-06-26.
> Reading this first saves you 15 minutes of bumping into things.

## What to read, in what order

Reading is organised by **moment of use** — what matters changes whether you're orienting, about to write code, or doing architectural work. Skip ahead if a tier doesn't apply to your task.

> **If you only have time for 4 files**, read these and start work: `CLAUDE.md` (project rules) + `docs/PROJECT_STATE.md` (sprint state) + `docs/development/CODING_STANDARDS.md` (how to write code that fits) + `docs/development/DOCUMENTATION_WORKFLOW.md` (mandatory updates on ticket close). Everything else is reference.

### Tier 1 — Orient (~5 min, must-read every session)

1. **`CLAUDE.md`** (root, committed) — project-level rules: English-only, retry guarantees, the 12 critical rules around `APP_CRYPTO_KEY`/`smartsheet_tokens`/webhook challenge timing/BIGINT IDs/org isolation, plus the list of active vs legacy backends. Read first for the "what's allowed" boundary. **Caveat:** its "Contexto de sesión actual" section is stale (still claims smarthub_core does not exist and Phase 1 not started). For current state defer to `docs/PROJECT_STATE.md`.
2. **`docs/PROJECT_STATE.md`** (local, gitignored) — current sprint state: open PRs, recent merges, in-flight work, blockers, next concrete actions, key product decisions, customer impact. The moving front line of the project.
3. **`docs/development/DOCUMENTATION_WORKFLOW.md`** — **mandatory** checklist for updating docs when closing a ticket or session. Enforced by `.cursor/rules/040-documentation-on-close.mdc`.
4. **`docs/development/AI_TOOL_ROUTING.md`** — when to use Cursor Ultra vs Claude Max 5x. Enforced by `.cursor/rules/050-ai-tool-routing.mdc`.

### Tier 2 — Before you write a single line of code (must-read before any non-trivial change)

These four are the "how to write code that fits this codebase" docs. Ignoring them produces code that gets bounced in review for boring style reasons or breaks invariants the team relies on.

5. **`docs/development/CODING_STANDARDS.md`** (782 lines) — rules, patterns, best practices. Covers retry helpers, logging conventions, dependency injection for testability, file organization per backend, naming conventions, async vs sync paths, error handling shape. This is the densest doc on the list and the highest-leverage second read after CLAUDE.md.
6. **`docs/development/CONSTRAINTS.md`** (248 lines) — restrictions: what you CANNOT touch without coordination across backends. Shared schemas (`smartsheet_tokens`, `user_profiles`), shared keys (`APP_CRYPTO_KEY`), shared library boundaries (`smarthub_core`). Each constraint includes the cost of violating it.
7. **`docs/architecture/DEPENDENCY_MAP.md`** (146 lines) — what breaks what. Before modifying anything cross-backend, look here to know which other tools are downstream of the change. Saves "I didn't realise touching X broke Y" incidents.
8. **`docs/development/AGENT_ONBOARDING.md`** — this file.
9. **`docs/development/TEMPLATES.md`** (393 lines) — code templates for workers, FastAPI routes, tests, Supabase repos. Use these as starting points so new code matches conventions automatically; less drift, less review back-and-forth.

### Tier 3 — Architectural context (read if doing cross-tool or platform work)

10. **`docs/architecture/TARGET_ARCHITECTURE.md`** (475 lines) — the platform we're migrating toward (Cloudflare → Webhook Gateway → Redis Streams → workers; smarthub_core as shared library). Read this if your task touches the migration boundary.
11. **`docs/architecture/CURRENT_STATE.md`** (refreshed 2026-06-25) — per-backend status: which uses smarthub_core, which has Redis dependency, port mapping, recent significant changes.
12. **`docs/architecture/MIGRATION_PLAN.md`** (957 lines) — phase-by-phase plan with checkboxes. Phase 0 done, Phase 1 in flight via PR #160, Phase 2+ not started. Tells you "is this work in scope of an active phase, or am I going off-roadmap?"
13. **`docs/architecture/PULSE_DESIGN_RATIONALE.md`** (632 lines) — the *why* behind the multi-provider design (Smartsheet first, HubSpot/Slack/Stripe/Twilio later). Read this only if your work is platform-agnostic infrastructure, not Smartsheet-specific.

### Tier 4 — Reference (look up as needed, don't pre-read)

- **`docs/tools/TOOL_REGISTRY.md`** — per-tool ports, env vars, tables. The source of truth when wiring up local dev for a backend you haven't run before.
- **`docs/infrastructure/ENVIRONMENTS.md`** — Supabase sandbox/staging/prod, AWS staging account, Git release train, local Redis, MCP defaults. **Read before deploys.**
- **`docs/development/JIRA_EPICS.md`** — Jira epic/task layout, ticket numbering conventions.

### Subsystem dives (only when relevant)

- **`docs/confluence/20-architecture/adr/`** — Architecture Decision Records. Each numbered ADR captures one decision with its alternatives and tradeoffs. Recent sprint context: ADR-004 (`004-elasticache-valkey.md`), ADR-022 (`022-backfill-estimate-draft-config.md` — SH-144), ADR-023 (`023-name-map-t3-semantic.md` — name_map T3). Numbers extend through 023; check the directory listing before allocating new numbers.
- **`docs/confluence/70-runbooks/`** — operational runbooks (one per scenario). Read `elasticache-provisioning.md` if doing SH-119 deploy work.
- **`docs/confluence/40-incidents/`** — postmortems. Read `2026-06-16-beth-cellimages.md` for the most recent customer-facing incident; covers root causes, fix, recovery, and the 4-5 actionable lessons that came out of it.
- **Backend-specific docs** under `backend/<tool>/docs/` — technical references owned by that tool. For i2c-3: `RUNTIME_VS_ESTIMATOR.md` covers the estimator/runtime parity contract.

## Project shape (high-level tree)

```
smarthub-main/
├── CLAUDE.md                          # project rules (committed, team-shared)
├── backend/
│   ├── image-to-cell-3/               # ACTIVE — current sprint focus, port 3011 dev
│   │   ├── src/                       # FastAPI app
│   │   ├── tests/unit/                # pytest, ~240 tests
│   │   ├── docs/                      # LOCAL only, technical reference
│   │   ├── infrastructure/            # CloudFormation snippets
│   │   ├── CHANGELOG.md               # per-tool changelog
│   │   └── venv/                      # local venv (gitignored)
│   ├── bulk-attachment-uploader/      # ACTIVE
│   ├── cell-history/                  # ACTIVE polling 30s
│   ├── attachment-counter/            # ACTIVE polling 70s
│   ├── dynamic-ddl/                   # ACTIVE polling 90s
│   ├── textnow/                       # ACTIVE webhook partial
│   ├── contact_book_connects/         # ACTIVE nightly sync
│   ├── id-checker/                    # ACTIVE shared picker
│   ├── encrypt-tool/                  # ACTIVE shared crypto
│   ├── image-to-cell/                 # LEGACY — ignore
│   ├── image-to-cell_2/               # LEGACY — ignore
│   └── image-to-cell-donotuse/        # LEGACY — ignore
├── frontend/                          # React, npm
├── shared/
│   └── smarthub_core/                 # shared library (currently consumed by i2c-3)
├── infra/
│   └── elasticache/                   # CloudFormation for ElastiCache Valkey (SH-119, local)
├── scripts/                           # PowerShell deploy helpers + Python smoke tests
├── docs/                              # LOCAL only — gitignored
│   ├── PROJECT_STATE.md
│   ├── architecture/
│   ├── confluence/
│   └── development/
└── .claude/
    └── settings.local.json            # personal Claude Code settings (gitignored)
```

## Where to find the answer to common questions

### Project state

| Question | Answer source |
|---|---|
| What backends are active? | `CLAUDE.md` "Herramientas activas" |
| What ports does each use? | `docs/tools/TOOL_REGISTRY.md` (authoritative), `CLAUDE.md` for high-level list |
| What's in PR right now? | `docs/PROJECT_STATE.md` OR `gh pr list` |
| What just merged this sprint? | `docs/PROJECT_STATE.md` OR `gh pr list --state merged --limit 20` |
| What's the Redis story today? | Local in-memory fallback. ElastiCache Valkey infra ready in SH-119 branch (not deployed). `docs/architecture/MIGRATION_PLAN.md` Phase 0.3 has the deploy steps. |
| Whose call is X? | "Owners" section below |

### Writing code (the "before I commit" questions)

| Question | Answer source |
|---|---|
| How should this method be structured? Async vs sync? Logging shape? Retry pattern? | `docs/development/CODING_STANDARDS.md` |
| Can I modify this shared table/key/library? | `docs/development/CONSTRAINTS.md` (says yes / no / "yes if you do these 3 things first") |
| Is there a template for a new FastAPI route / worker / test? | `docs/development/TEMPLATES.md` |
| If I change X, what else breaks? | `docs/architecture/DEPENDENCY_MAP.md` |
| What's the convention for branch naming, PR titles, commit messages? | `CLAUDE.md` + the "PR / review flow" section below |

### Architecture and design

| Question | Answer source |
|---|---|
| Why was X decided this way? | `docs/confluence/20-architecture/adr/` (numbered ADRs) |
| What's the target architecture and why? | `docs/architecture/TARGET_ARCHITECTURE.md` + `docs/architecture/PULSE_DESIGN_RATIONALE.md` |
| Which migration phase is this work in? | `docs/architecture/MIGRATION_PLAN.md` |
| How is i2c-3 wired today (vs the target)? | `docs/architecture/CURRENT_STATE.md` |
| How does the i2c-3 backfill billing work? | `backend/image-to-cell-3/docs/RUNTIME_VS_ESTIMATOR.md` |

### History

| Question | Answer source |
|---|---|
| What broke when, and how was it fixed? | `docs/confluence/40-incidents/` |
| What was the Beth incident about? | `docs/confluence/40-incidents/2026-06-16-beth-cellimages.md` |
| What lessons learned came out of past incidents? | Same folder; each postmortem has a "Lessons learned" section near the bottom |

## Local dev setup pointers

Don't duplicate content here — point to where it lives:

- **Per-backend `.env`**: each backend folder has its own `.env` (gitignored). Copy from `.env.example` if it exists. Variables typically include: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `APP_CRYPTO_KEY`, `SMARTSHEET_*`, `REDIS_URL` (post-SH-119).
- **Backend venv**: typically `backend/<tool>/venv/Scripts/python.exe` on Windows. Activate with `.\venv\Scripts\Activate.ps1` in PowerShell. Install deps via `pip install -r requirements.txt -r requirements-dev.txt`.
- **Run a backend locally**: `uvicorn src.main:app --reload --port <port>` from the backend folder with venv activated.
- **Local Redis**: `docker run -d --name smarthub-redis -p 6380:6379 redis:7-alpine` (port 6380 to avoid clash with any other Redis on 6379).
- **Frontend**: `npm install` + `npm run dev` in `frontend/`.
- **Tests**: `backend/<tool>/venv/Scripts/python.exe -m pytest backend/<tool>/tests/unit -q` from repo root.

## PR / review flow

- Branch from: `develop`.
- Branch naming: `SH-<num>-<kebab-description>` (e.g., `SH-144-i2c-backfill-estimate-draft-config`). For hotfixes: `hotfix-YYYY-MM-DD-<short-name>`.
- Base PR against: `develop`.
- Release flow: `develop` → `test` (PR by Steven, e.g. PR #170) → `main` (PR by Steven, e.g. PR #171).
- All commits in English. No `Co-Authored-By: Claude` trailers (project rule).
- No `--no-verify`, no force-push without explicit ask from the user.
- `gh` CLI: have at least two GitHub accounts configured (`Renevc591` for main repo access, `Renevc14` as fallback). Use `gh auth switch --user Renevc591` if push fails with "Repository not found."

## Owners and ping channels

- **@rene** (the user of this repo): primary developer.
- **@steven**: infra, release cuts, AWS.
- **@AK**: frontend lead. Currently owns SH-122 (the consumer of SH-144).

(Adjust these if you know other contributors active in the current sprint.)

## Gotchas to know before touching code

- **`APP_CRYPTO_KEY` shared across tools**: never change it without coordinating simultaneous restart of ALL backends that read `smartsheet_tokens` (i2c-3, bulk-attachment-uploader, encrypt-tool, etc.) AND rotating all tokens in the DB. Listed as Critical Rule #5 in `CLAUDE.md`.
- **`smartsheet_tokens` table schema is shared**: never modify without coordinated migration across all 8 backends. Critical Rule #6.
- **`user_profiles.organization_id`**: never modify without reviewing impact on all backends. Critical Rule #7.
- **`crypto_helper.py` exists copied in 6 backends**: changing one does NOT change the others. Critical Rule #8.
- **Webhook challenge response must be <5s**: any `/webhook/*` endpoint must respond fast. NEVER put heavy logic in `Depends()` for webhook routes. Critical Rule #9.
- **Smartsheet IDs are BIGINT (64-bit)**: never use INTEGER in DB for `row_id`, `column_id`, `worksheet_id`. Critical Rule #10.
- **`Smartsheet-Change-Agent` header is required** on any Smartsheet write to prevent infinite webhook loops. Critical Rule #11.
- **Org isolation**: always scope DB queries to `organization_id`. Critical Rule #12.
- **`includeAll` is deprecated on Smartsheet `/webhooks`** (errorCode 1018). Fix landed in PR #161 for i2c-3 fleet-wide. NON-i2c-3 backends (cell-history, encrypt-tool, bulk-attachment-uploader, smarthub_core/picker) still have stale `includeAll` usage — cross-fleet cleanup pending.
- **English only on engineering content**: `CLAUDE.md`, ADRs, code comments, commits, PR descriptions. Spanish is fine in chat (Rene's preferred conversation language).

## Quick orientation commands

When you join a session, run these to know where you are:

```powershell
git branch --show-current             # what branch am I on?
git log --oneline -10                 # what are the last 10 commits?
gh pr list --state open --limit 5     # what's open?
gh pr list --state merged --limit 5   # what just merged?
```

## Where AI configs live

- **`CLAUDE.md`** (root, committed): project-level rules visible to all agents.
- **`.cursor/rules/*.mdc`** (gitignored): Cursor rules — includes `040-documentation-on-close` and `050-ai-tool-routing`.
- **`.claude/settings.local.json`** (gitignored): personal Claude Code settings.
- **`docs/`** (gitignored): local agent-consumable docs. Index: `docs/README.md`.
- **`AGENTS.md`**, **`backend/**/AGENTS.md`** (gitignored): per-repo / per-tool agent context.

## How to update local docs/

**Mandatory on every ticket close** — see `docs/development/DOCUMENTATION_WORKFLOW.md` and `.cursor/rules/040-documentation-on-close.mdc`.

Minimum: update `docs/PROJECT_STATE.md` + affected `backend/<tool>/CHANGELOG.md`. Edit affected sections only; date-stamp changes. Do not rewrite whole files.
