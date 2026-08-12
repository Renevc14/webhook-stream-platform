# Plan de Migración — Arquitectura Actual → Objetivo

> **Last reviewed:** 2026-07-11.
> Phase 0 complete (shared library merged via PRs #131 and #139; **ElastiCache staging deployed** `smarthub-redis-staging`).
> Phase 1 **merged** (PR #160 + PR #172 REDIS_URL wiring on `develop`). Staging validated: `/ready` → `redis_lua: true`, dedup + golden-path webhook OK.
> **SH-176 hotfix shipped prod 2026-07-10** (#180 + #183 on `main`). Post-deploy validation 2026-07-11: 0× `1004` in test window.
> **SH-159 (Cell History webhook)** — implementation complete on branch `sh-159/uprade/ch-webhook-upgrade` (Akber); **not merged**. Sandbox schema applied; direct-webhook MVP (no Gateway yet). See [Phase 4 — Cell History (SH-159)](#phase-4--cell-history-sh-159-in-progress-2026-07-11) below.
> **Pulse lean strategy (2026-07-06):** shared Webhook Gateway + Redis Streams + **monolithic backends** (1 ECS service per tool). Split `api` + `worker` per tool is **deferred** until metrics justify it. See [Pulse lean strategy](#pulse-lean-strategy-revised-2026-07-06) below.
> Epic **SH-146** = Phase 2 (I2C hardening + runtime flags). Epic **SH-174** = Phase 3 (Webhook Gateway + stream cutover). PRs **#173 (SH-148)** and **#174 (SH-150)** merged 2026-07-06. Jira URL cutover: **SH-175**.
> Progress tracker: [I2C_PHASE_2_PROGRESS.md](../infrastructure/I2C_PHASE_2_PROGRESS.md) · parallel work: [I2C_PHASE_2_PARALLEL_WORK.md](../infrastructure/I2C_PHASE_2_PARALLEL_WORK.md).

## Principios guía

1. **Migrar sin downtime.** Cada fase es independientemente deployable y reversible. Nunca romper una herramienta para arreglar otra.
2. **Customer-driven sequencing.** El orden viene del dolor del cliente, no de la elegancia arquitectónica. Si una fase no tiene métricas que la justifiquen, no se inicia.
3. **Off-ramps explícitas.** Cada fase Phase 2+ tiene criterios cuantitativos para decidir si es necesaria. Sin esos criterios, no se construye "por si acaso".
4. **`smarthub_core` ≠ Pulse architecture.** La librería compartida es un deliverable autónomo (centraliza código duplicado). Pulse (Gateway + Streams + Workers desacoplados) es restructure de runtime, requiere justificación independiente.
5. **Nunca migrar más de un backend por deploy.** Validar en staging antes de producción.
6. **Platform-agnostic desde día 1.** Smarthub se conectará progresivamente a múltiples proveedores externos (Smartsheet, HubSpot, Slack, Stripe, etc.). Toda capa que toque webhooks, tokens, o APIs externas se diseña con interfaces abstractas y módulos por provider — no con código Smartsheet-only que después haya que retrofitear.
7. **Lean infra by default.** Maximizar beneficio arquitectónico (gateway, streams, locks, dedup) sin duplicar ECS services por tool. Un servicio por tool + un gateway compartido hasta que métricas demuestren necesidad de escalar API y consumer por separado.

---

## Pulse lean strategy (revised 2026-07-06)

### Decisión

El plan original asumía **Phase 2 = multi-instance con split obligatorio** (`APP_MODE=api|worker-i2c`, dual ECS services por tool). Tras revisar costo AWS vs beneficio, la línea de trabajo única queda:

```
Phase 1 ✅ → I2C locks (SH-148/150) → Gateway v1 → Streams en monolito (I2C)
         → Attachment Info webhook → Cell History webhook
         → (solo si hace falta) split api/worker por tool
```

### Arquitectura intermedia (objetivo operativo)

```
Smartsheet → Webhook Gateway (1 ECS compartido)
                ↓ XADD
            Redis Streams (stream:i2c, stream:attachment-counter, …)
                ↓ XREADGROUP
            Cada tool (1 ECS): HTTP API + stream consumer embebido
            Opcional: reconciliation poll lento (10–15 min) como red de seguridad
```

### Qué cambia vs plan anterior

| Aspecto | Plan anterior | Plan lean (actual) |
|---|---|---|
| ECS por tool | 2 services (`api` + `worker`) | **1 service** (default: API + consumer en mismo proceso) |
| `APP_MODE` | Split obligatorio `api` vs `worker-i2c` | **Runtime role flags** (`RUN_HTTP_API`, `RUN_STREAM_CONSUMER`); default ambos `true` |
| Gateway | Phase 3, después de dual ECS | **Antes del cutover I2C**; +1 ECS compartido, no por tool |
| Multi-réplica smoke (SH-149) | Gate principal | **Opcional** hasta `desiredCount > 1` |
| Dual ECS staging (SH-154) | Requerido | **Diferido** — label `deferred-scale-out` |
| Polling → webhook | Cell History primero | **Attachment Info primero** (70s poll, ROI más alto) |

### Costo AWS relativo

| Escenario | Servicios ECS extra | Costo relativo |
|---|---|---|
| Plan viejo (gateway + api + worker × N tools) | +2N + 1 | Alto |
| **Plan lean** (gateway + 1 service × N tools) | **+1 gateway** | Medio-bajo |

Redis (ElastiCache) ya provisionado; el incremento principal es **un gateway ECS** (~512MB–1GB), no duplicar cada backend.

### Jira epics (2026-07-06 — Phase 2 / 3 split)

| Epic | Phase | Scope |
|---|---|---|
| **SH-146** | Phase 2 | I2C hardening + runtime flags |
| **SH-174** | Phase 3 | Webhook Gateway + stream cutover |

Trackers: [I2C_PHASE_2_PROGRESS.md](../infrastructure/I2C_PHASE_2_PROGRESS.md) · [I2C_PHASE_3_PROGRESS.md](../infrastructure/I2C_PHASE_3_PROGRESS.md)

#### SH-146 — Phase 2 tickets

| Ticket | Notes |
|---|---|
| **SH-147** | Gate Phase 1 (~1 week stable post SH-148/150) |
| **SH-148** | Backfill lock — merged PR #173 |
| **SH-149** | Multi-replica dedup smoke (optional) |
| **SH-150** | Stampede lock — merged PR #174 |
| **SH-151** | Runtime role flags; monolith default |
| **SH-154** | Dual ECS (deferred) |

#### SH-174 — Phase 3 tickets

| Ticket | Plan | Notes |
|---|---|---|
| **SH-172** | 3.1 | Gateway scaffold + Smartsheet handler |
| **SH-173** | 3.2 | Gateway lifecycle manager |
| **SH-153** | 2.5/3 | Gateway publishes to `stream:i2c` |
| **SH-152** | 3.5 | I2C embedded stream consumer |
| **SH-175** | 3.4 | Smartsheet URL cutover (I2C first) |
| **SH-155** | 3.3 | platform-events + stream DLQ (optional) |
| **SH-156** | — | Staging E2E smoke |
| **SH-157** | — | Production deploy |

#### Phase 4 (polling → webhook)

| Scope | Jira | Status (2026-07-11) | Estimado restante |
|---|---|---|---|
| Attachment Info webhook migration | — (no epic) | Not started | 1–2 sem |
| Cell History webhook migration | **SH-159** | **In progress** — branch ready for review | Merge + deploy ~1 sem |

---

## Phase 4 — Cell History (SH-159, in progress 2026-07-11)

Akber's [SH-159](https://smarthub-dev.atlassian.net/browse/SH-159) delivers most of the planned Cell History webhook migration **before** I2C Gateway cutover (SH-174).

### What shipped on branch (not merged)

- New `backend/cell-history-2/` — webhook receive, lifecycle reconciler, backfill (`first_run` / `baseline_seed`), DLQ, billing heartbeat, 143 unit tests.
- Per-config `processing_mode` (`polling` | `webhook`) on `cell_history_configurations`.
- Tables: `cell_history_webhooks`, `cell_history_failed_events`.
- Legacy `cell-history` filters `processing_mode='polling'` only.
- Frontend: create-config writes `processing_mode` (default `webhook`); edit fires reformat / backfill-columns.

### Schema rollout status

| Environment | Migrations applied |
|---|---|
| Sandbox | ✅ (columns + tables present) |
| Staging | ❌ |
| Production | ❌ |

### Relationship to Pulse (SH-146 / SH-174)

```
Today (I2C prod, CH-2 MVP):  Smartsheet ──webhook──► tool ECS (direct)
Pulse target (Phase 3):        Smartsheet ──webhook──► Gateway ──XADD──► stream:{tool} ──► worker
```

SH-159 **does not block** I2C Phase 2/3. Recommended sequence:

1. Finish I2C Phase 2 staging + SH-147 gate + Phase 3 Gateway for **I2C** (SH-175).
2. Merge/deploy SH-159 — CH-2 on **direct webhook URL** (same interim pattern as I2C prod).
3. Gradual config flip polling → webhook per org.
4. **Later ticket:** CH Gateway cutover → `stream:cell-history` (mirror SH-175 for I2C).

CH-2 MVP constraints align with I2C Phase 1: **Redis-free**, `WORKERS=1`, in-memory dedup. Redis wiring (Rene) unlocks horizontal scale for both.

### Merge gate (code review 2026-07-11)

Fix before prod: Change-Agent on `update_cells_bulk`; org-ownership on user-facing `/v1/configs/*` routes.

---

## Concurrencia en I2C — gaps que la migración cierra

I2C v3 hoy corre con concurrencia masiva (hasta ~640 ops simultáneas por ECS task: 4 uvicorn workers × asyncio event loop × ThreadPoolExecutor de backfill × concurrent orgs/rows/images). La memoria es per-proceso, lo que abre gaps específicos:

| Gap actual | Por qué pasa | Cómo se resuelve |
|---|---|---|
| Dedup in-memory (`_processing_cache`, `_row_cache`, `_completed_attachments`) solo funciona dentro de un uvicorn worker | Cada proceso tiene su propio dict — Python multi-process no comparte memoria | Phase 2.1: Redis `dedup_set` cross-process |
| `_db_semaphore` per-proceso no bound el pool global de DB | Igual | Phase 1.3 (httpx pool acotado en `smarthub_core.db.supabase`) |
| `_usage_cache` per-proceso con 30s TTL → cluster overshoot del bolt cap por hasta 4×30s × throughput | Cache local | Phase 2.2: cache via Redis o invalidación cross-process |
| Rate limiter per-worker → 4 workers × 275/min = 1100/min cluster vs 300/min real Smartsheet | Per-process token bucket | Phase 1.4: `smarthub_core.platforms.smartsheet` Lua sliding window cluster-wide |
| Concurrent backfills sobre mismo config → doble billing | Sin lock | Phase 2.3: `acquire_lock` Redis con token + heartbeat |
| Webhook + backfill simultáneos sobre mismo row | Sin coordinación cross-path | Cierra automático con #25 full fix (UNIQUE constraint, Phase 1.1) |
| `ThreadPoolExecutor` + `asyncio` + sesión Smartsheet compartida | Thread-safety del SDK no garantizada | Phase 1.4: `asyncio.to_thread` por call (sesión usada por un thread a la vez) |
| PreparedRequest cacheado fuera del retry (#17) | Stream consumido en primer intento | Phase 1.4 backport o migración |

**Implicación:** la migración no es solo cleanup arquitectónico. La librería sola (Phase 0–1) cierra ~70% de los gaps. Pulse architecture (Phase 2+) cierra el resto cuando la escala lo justifica.

---

## Multi-provider design — Pulse universal

A 5-8 herramientas nuevas/año más conexiones progresivas a Smartsheet, HubSpot, Slack, Stripe, Twilio y otros providers, Pulse se diseña **polyglot desde día 1**. Las consecuencias arquitectónicas son concretas:

### Lo que NO cambia per provider
- `smarthub_core.crypto` — los tokens de cualquier provider se encriptan con el mismo `APP_CRYPTO_KEY`
- `smarthub_core.db.redis` — primitives (cache, dedup, locks, streams, rate limit) son provider-agnostic
- `smarthub_core.db.supabase` — un solo backend de DB
- `smarthub_core.worker.base` — workers consumen de streams sin saber qué provider los originó
- Envelope versionado de eventos (`{v, event_id, tool, org_id, received_at, raw}`) — solo `raw` cambia su shape per provider; lo demás es uniforme
- Estructura de billing (bolts, usage_log) — provider-agnostic

### Lo que cambia per provider

| Aspecto | Smartsheet | HubSpot | Slack | Stripe | Twilio |
|---|---|---|---|---|---|
| **Algoritmo de firma** | HMAC-SHA256(body) | HMAC-SHA256(secret + method + uri + body + ts) | HMAC-SHA256("v0:"+ts+":"+body) | HMAC-SHA256(ts+"."+body) versioned | HMAC-SHA1(URL+sorted-params) |
| **Header de firma** | `Smartsheet-Hmac-SHA256` | `X-HubSpot-Signature-V3` | `X-Slack-Signature` | `Stripe-Signature` | `X-Twilio-Signature` |
| **Challenge** | Sync challenge en registro | Ninguno (URL debe ser 2xx <10s) | URL verification con `challenge` field | Ninguno | Ninguno |
| **Token model** | Long-lived PAT | OAuth + refresh token + expiry | Bot token + user token + OAuth | API key (sin OAuth typical) | Account SID + auth token |
| **Rate limit** | 300/min global | 100-150/sec per portal | Tiered per channel | 100/sec read+write | Variable per resource |
| **Refresh tokens** | No | Sí (cada ~6h) | No (long-lived) | No | No |

Cada provider necesita su propio módulo de cliente HTTP, su propia validación de webhook, su propio rate limit key. Pero todos enchufan a la misma infraestructura compartida (cache, locks, streams, worker base).

### Estructura de `smarthub_core` con multi-provider

```
shared/smarthub_core/src/smarthub_core/
├── crypto/                          (provider-agnostic)
├── db/
│   ├── redis.py                     (provider-agnostic)
│   └── supabase.py                  (provider-agnostic)
├── platforms/                       ← módulos por provider
│   ├── shared/
│   │   ├── token_resolver.py        interface abstracta `TokenResolver(org, slug, provider) → token`
│   │   ├── webhook_base.py          `BaseWebhookHandler` con verify_signature() abstract
│   │   └── oauth.py                 helpers de refresh para providers con OAuth
│   ├── smartsheet/
│   │   ├── client.py                ← era smarthub_core.smartsheet.client
│   │   └── webhook.py               HMAC verification + challenge response
│   ├── hubspot/
│   │   ├── client.py
│   │   ├── webhook.py
│   │   └── oauth.py                 token refresh logic
│   ├── slack/
│   │   ├── client.py
│   │   └── webhook.py               URL verification con `challenge` field
│   ├── stripe/
│   │   ├── client.py
│   │   └── webhook.py
│   └── ...
├── worker/                          (provider-agnostic)
└── settings.py
```

**Esta restructure es prerequisito de Phase 1.2** (i2c migration). Hacerla AHORA, antes que ningún backend importe `smarthub_core.smartsheet.*`, es zero-cost. Hacerla DESPUÉS implica retrofitear imports en cada backend ya migrado.

### Tabla de tokens — schema multi-provider

Dos opciones:

**Opción A — Tabla por provider (recomendada para clarity)**

```sql
-- Existente
smartsheet_tokens (org_id, product_slug, encrypted_token)

-- Nuevas (cuando agregue cada provider)
hubspot_tokens (org_id, product_slug, encrypted_access_token, encrypted_refresh_token, expires_at, scope)
slack_tokens   (org_id, product_slug, encrypted_bot_token, encrypted_user_token, team_id, scope)
stripe_tokens  (org_id, product_slug, encrypted_api_key, account_id, livemode)
```

`smarthub_core.platforms.shared.token_resolver` provee una interface unificada que internamente sabe a qué tabla ir según `provider`.

**Opción B — Tabla genérica `external_tokens`**

```sql
external_tokens (
  org_id, product_slug, provider,
  encrypted_token,
  encrypted_refresh_token NULL,
  expires_at NULL,
  metadata JSONB        -- account_id, team_id, scope, etc.
)
```

Más simple pero pierde tipo-safety y requiere unpacking del JSONB para campos provider-specific.

**Recomendado: Opción A** — tablas explícitas para los providers que ya conocemos, schema migrations claras, menos magic. La interface unificada de smarthub_core hace que el caller no note la diferencia.

### Webhook Gateway polyglot — diseño desde día 1

URL pattern: `POST /webhook/{provider}/{tool_slug}/{org_id}`

```
gateway/
├── handlers/
│   ├── smartsheet.py    parse signature, verify HMAC, extract event, normalize to envelope
│   ├── hubspot.py       parse signature with timestamp, OAuth refresh if needed
│   ├── slack.py         handle URL verification challenge, parse signature
│   ├── stripe.py        verify Stripe-Signature with versioning, parse event
│   └── ...
├── routing.py           URL → handler dispatch via registry
├── lifecycle.py         provider-agnostic webhook lifecycle (registración, renovación, zombies)
└── publishers.py        common: write envelope to stream:{provider}:{tool} or stream:{tool}
```

Decision: **publish a `stream:{tool}`** (no incluir provider en stream key). Así un tool que consuma de múltiples providers tiene un solo stream. El envelope tiene `provider` field.

Envelope ampliado:
```json
{
  "v": "1",
  "event_id": "uuid-v4",
  "provider": "smartsheet" | "hubspot" | "slack" | "stripe",
  "tool": "i2c",
  "org_id": "...",
  "config_id": "...",
  "received_at": "2026-05-04T...",
  "event_type": "row_added" | "contact.created" | etc.,
  "raw": { ...provider-specific shape... }
}
```

Workers parsean `provider` + `event_type` para decidir qué hacer.

### Rate limiting cluster-wide — keyed por (provider, org, slug)

```
ratelimit:smartsheet-api:{org}:{slug}    (275/min)
ratelimit:hubspot-api:{org}:{slug}       (100/sec)
ratelimit:slack-api:{org}:{slug}         (per-channel tiers — más complejo)
ratelimit:stripe-api:{org}:{slug}        (100/sec)
```

El primitive Lua sliding window (en `smarthub_core.db.redis`) es el mismo. Los límites y keys se definen per provider en `smarthub_core.platforms.{provider}.client.RATE_LIMIT_*`.

### Cross-provider events — el bus se vuelve esencial

Caso típico: Smartsheet webhook → tool A procesa imágenes → publica `image_processed` → tool B (conectado a HubSpot) sube el resultado a un Contact en HubSpot.

Sin platform-events bus, tool B no tiene forma limpia de enterarse del evento (acoplamiento implícito o polling). Con el bus, B subscribe a `stream:platform-events` filtrando `type=image_processed` y reacciona.

**A multi-provider, el platform-events bus pasa de "Phase 5 condicional" a "Phase 3 esencial".** Es el habilitador de cross-provider workflows. Ver Phase 3.3.

### Playbook — agregar un nuevo provider

Una vez la arquitectura polyglot esté en su lugar (Phase 0.2.a + Phase 3.1), agregar un nuevo provider sigue un patrón reproducible. Estimación: 2-3 semanas full por provider, dependiendo de la complejidad de su modelo de auth y webhook.

**Paso 1 — Schema (1-2 días)**
- [ ] Crear tabla `{provider}_tokens` en Supabase con UNIQUE(org_id, product_slug). Campos: `encrypted_token`, `encrypted_refresh_token NULL`, `expires_at NULL`, `metadata JSONB` para account_id/team_id/scope provider-specific.
- [ ] Migration coordinada con todos los backends que usan `smarthub_core`.

**Paso 2 — `smarthub_core.platforms.{provider}` (3-5 días)**
- [ ] `client.py` — wrapper async sobre el SDK del provider, con cache-aside del token, rate limit cluster-wide via `ratelimit:{provider}-api:{org}:{slug}`, retry de errores transitorios típicos del provider (4xx con `error_code` recuperable, HTTP/2 GOAWAY, timeouts).
- [ ] `webhook.py` — implementación de `BaseWebhookHandler`: `verify_signature` con el algoritmo del provider (ver tabla en sección "Lo que cambia per provider"), `extract_event`, `normalize_envelope` agregando `provider` y `event_type` al envelope.
- [ ] `oauth.py` (solo si el provider usa OAuth) — refresh token logic. Activado por `expires_at - now() < 60s`. Lock `lock:oauth-refresh:{org}:{slug}` para evitar refresh race.
- [ ] `webhook_lifecycle.py` — implementación de `WebhookLifecycle`: register, renew, list_zombies, delete. Cada provider tiene su propia API de webhook lifecycle.
- [ ] Tests unitarios mínimos: signature verification con vectors reales del provider, rate limit, oauth refresh si aplica, webhook lifecycle CRUD.

**Paso 3 — Webhook Gateway handler (1-2 días)**
- [ ] Agregar `services/webhook-gateway/handlers/{provider}.py` que registra el handler en el dispatch registry.
- [ ] Agregar challenge handling si el provider lo requiere (Slack URL verification, Smartsheet challenge, etc.). HubSpot/Stripe/Twilio no tienen challenge — el handler retorna 2xx directo.
- [ ] Test end-to-end: webhook real del provider (sandbox) llega al gateway, validate firma, XADD a `stream:{tool}` con envelope correcto.

**Paso 4 — Token rotation flow + admin UI (2-3 días)**
- [ ] Endpoint en `api-gateway` para rotar/registrar token del nuevo provider. Patrón estándar: write DB → DEL cache (`{provider}-token:{org}:{slug}`) → return.
- [ ] OAuth flow si aplica: callback URL, token exchange, store encrypted refresh+access+expires_at.
- [ ] Frontend: pantalla de connect-to-{provider} en admin UI.

**Paso 5 — Primer tool consumiendo del provider (variable)**
- [ ] Worker que consume `stream:{tool}` filtrando `envelope.provider == "{provider}"`.
- [ ] Lógica de procesamiento específica del tool.
- [ ] Tests integración con sandbox del provider.

**Paso 6 — Documentación + observabilidad (1 día)**
- [ ] Update `docs/tools/smarthub_core/USAGE.md` con ejemplos del nuevo provider.
- [ ] Update `docs/tools/TOOL_REGISTRY.md` con la nueva tabla de tokens.
- [ ] Métricas: queue depth, error rate, rate limit hits, webhook validate latency — keyed por `provider` para dashboards comparativos.

**Lo que NO toca este playbook (zero modification):**
- `smarthub_core.crypto`, `smarthub_core.db.redis`, `smarthub_core.db.supabase`, `smarthub_core.worker.base`
- Webhook gateway core (routing, lifecycle manager singleton, publishers)
- Stream envelope schema (solo cambia `raw` y `event_type`)
- Bolt cap / billing structure
- Admin UI shell

Si en el camino notás que algo de la lista anterior necesita modificarse para acomodar el provider, **es señal de que la abstracción de smarthub_core no está completa** — patch la abstracción antes de continuar, sino se acumula deuda multi-provider.

---

## Phase 0 — Foundation (sin riesgo, semana 1)

**Objetivo:** preparar la infraestructura compartida sin tocar ningún backend existente.

### 0.1 — ~~PgBouncer enable~~ → **N/A para los backends actuales**

**Status:** descartada el 2026-05-06 tras auditoría de uso de Postgres en los backends.

**Razón:** Phase 0.1 originalmente proponía habilitar PgBouncer en modo Transaction y reemplazar `SUPABASE_URL` por el puerto 6543 del pooler. Ese cambio solo aplica a clientes que abren conexiones Postgres directas (`psycopg`, `asyncpg`, `sqlalchemy`). Búsqueda exhaustiva en los 14 backends (`backend/*/requirements.txt`, `pyproject.toml`, imports en `*.py`, strings de conexión `postgres://`/`DATABASE_URL`):

```
0 menciones de psycopg | asyncpg | sqlalchemy | sqlmodel | databases
0 strings postgres:// | postgresql:// | DATABASE_URL | POSTGRES_URL
```

Todos los backends acceden a Supabase **únicamente vía supabase-py → PostgREST → REST**. El pooler externo de Supabase no se interpone en ese path — PostgREST mantiene su propio pool interno hacia Postgres. Cambiar la URL del cliente a `pooler.supabase.com:6543` rompería el cliente (es un endpoint Postgres, no REST).

**Lo que sí ataca el síntoma original** (`RemoteProtocolError: Server disconnected` durante backfills):

Esos errores ocurren en el pool **httpx** del cliente Python contra PostgREST, no en el lado de Postgres. La solución correcta vive en `smarthub_core.db.supabase`:
- Pool httpx acotado explícitamente (`SUPABASE_MAX_CONNECTIONS`, `SUPABASE_MAX_KEEPALIVE`, `SUPABASE_KEEPALIVE_EXPIRY_S`)
- Retry async con `asyncio.sleep` (no bloquea event loop) + `RETRYABLE_PATTERNS` que incluye `RemoteProtocolError` / `Server disconnected` / GOAWAY

Esos fixes ya están en SH-9. **Phase 0.1 efectiva = Phase 1.3** (i2c supabase migration), donde los backends adoptan `smarthub_core.db.supabase` y heredan estos settings.

**Cuándo re-activar Phase 0.1 original:** si en el futuro algún backend introduce `psycopg`/`asyncpg`/`sqlalchemy` para queries que PostgREST no puede expresar, ahí sí hay que apuntar ese cliente al Transaction pooler (puerto 6543) — pero solo ese cliente, no `SUPABASE_URL` global.

### 0.2 — `smarthub_core` library (DONE — merged 2026-05-13)

Estado al 2026-06-25: SH-9 mergeada via **PR #131** (2026-05-13). SH-14 (i2c-3 adopción crypto + Supabase) mergeada via **PR #139** (2026-05-13). La librería está consumida por i2c-3 en producción y Phase 1 (PR #160) la profundiza.

**Branch:** `SH-9-smarthub_core-python-library` — 6 commits sobre develop, 43 archivos, ~6500 insertions, 189 tests passing (5 skipped Layer B opt-in).

#### 0.2.a — Restructurar `smarthub_core` para multi-provider (DONE 2026-05-07)

Hecho en commit `7648e31`:

- [x] Movido `src/smarthub_core/smartsheet/` → `src/smarthub_core/platforms/smartsheet/`
- [x] Creado `src/smarthub_core/platforms/shared/`:
  - `token_resolver.py` — `TokenResolver` ABC + `ResolvedToken` dataclass + `TokenNotFoundError`
  - `webhook_base.py` — `BaseWebhookHandler` ABC + `ChallengeResponse` + `NormalizedEvent` + `WebhookSignatureError`
  - `oauth.py` — `OAuthRefresher` ABC + `RefreshedToken` + `should_refresh` helper (5-min default threshold)
- [x] Agregado `platforms/smartsheet/token_resolver.py` (`SmartsheetTokenResolver` — single source of truth for credential resolution; `SmartsheetClient` ahora delega)
- [x] Agregado `platforms/smartsheet/webhook.py` (`SmartsheetWebhookHandler` — HMAC SHA-256 con 63-char quirk + sync challenge)
- [x] Agregado `platforms/smartsheet/picker.py` (`SmartsheetPicker` — workspace cache TTL 300s + sheets/columns uncached)
- [x] Tests movidos a `tests/platforms/` y `tests/e2e/platforms/`. 85 tests nuevos para los módulos nuevos. Total: 182 unit + 7 e2e Layer A
- [x] Local docs en `docs/tools/smarthub_core/` actualizados (README, OVERVIEW, USAGE, MIGRATION, TESTING)

**Public API:** `from smarthub_core.platforms.smartsheet import SmartsheetClient, SmartsheetTokenResolver, SmartsheetWebhookHandler, SmartsheetPicker`. Provider-agnostic abstractions: `from smarthub_core.platforms.shared import TokenResolver, BaseWebhookHandler, OAuthRefresher, should_refresh`.

#### 0.2.b — Módulos entregados post-restructure

- `smarthub_core.crypto.token_cipher` — AES-GCM, wire-compatible con i2c-3 (provider-agnostic)
- `smarthub_core.db.redis` — RedisClient + helpers cache/dedup/locks/streams/rate-limit (provider-agnostic)
- `smarthub_core.db.supabase` — httpx pool acotado + retry async (provider-agnostic)
- `smarthub_core.platforms.smartsheet.client` — cache-aside token, rate limit, retry de 4000/GOAWAY
- `smarthub_core.platforms.smartsheet.webhook` — HMAC SHA-256 verification + challenge response (a implementar — Phase 3 prep)
- `smarthub_core.platforms.shared.token_resolver` — interface abstracta
- `smarthub_core.platforms.shared.webhook_base` — `BaseWebhookHandler`
- `smarthub_core.platforms.shared.oauth` — helpers reutilizables
- `smarthub_core.worker.base` — abstract SmarthubWorker
- `smarthub_core.worker.health` — compute_health_report
- `smarthub_core.settings` — CoreSettings via pydantic-settings

**Tests:** 182 unit + 11 e2e (7 Layer A + 4 Layer B opt-in) tras restructure. Verde al 2026-05-07.

**Documentación:** `docs/tools/smarthub_core/` actualizada con la estructura `platforms/`.

### 0.3 — Provisionar AWS infra para Redis (ElastiCache for Valkey 7.2)

Estado al 2026-06-25: infra-as-code completa en branch local **SH-119-i2c-elasticache-valkey-provisioning** (commit `ecef105`, NOT YET PUSHED). Bloqueado por credenciales SSO del usuario para resolver VPC + subnet + SG ids reales antes de `sam deploy`.

ADR de referencia: [docs/confluence/20-architecture/adr/004-elasticache-valkey.md](../confluence/20-architecture/adr/004-elasticache-valkey.md) (Accepted, 2026-05).
Runbook de provisioning: [docs/confluence/70-runbooks/elasticache-provisioning.md](../confluence/70-runbooks/elasticache-provisioning.md) (395 líneas).

Artefactos ya escritos localmente:
- [x] `infra/elasticache/template.yaml` — CloudFormation para Valkey 7.2, posturas `staging` y `production`, parámetro `Environment` selecciona entre ellas
- [x] `infra/elasticache/samconfig.toml` — bloque `staging` con `<PLACEHOLDER>` para VPC, 2 subnets, ECS task SG; bloque `production` comentado
- [x] `scripts/smoke_test_redis.py` — raw redis-py, 6 checks (ping, server info, SET/GET/DEL, SET NX EX dedup, EVAL Lua, ZADD/ZREM sorted set ops). Validado localmente contra `docker run redis:7-alpine` = 6/6 pass.
- [x] `backend/image-to-cell-3/infrastructure/ecs-task-definition.redis-env.snippet.json` — reescrito con `staging_block` + `production_block` y secret naming `smarthub/redis/<env>`
- [x] Removidos: `backend/image-to-cell-3/infrastructure/elasticache-redis.yaml` (superseded), `elasticache-redis.parameters.example.json` (superseded por samconfig.toml)

Pasos pendientes del runbook (bloqueado por credenciales del usuario):
- [ ] Usuario corre `aws configure sso` con: SSO start URL `https://d-9c6758d91a.awsapps.com/start`, SSO region `us-east-1` (Identity Center), account `262252232181` (Smarthub Staging/Test), role `DeveloperAccess-PowerUser`, default client region `eu-west-2`, profile name `smarthub-staging` (matches `scripts/use-aws-smart-hub-staging.ps1`)
- [ ] Correr los 3 `describe-*` lookups: VPC id, dos private subnet ids en distintas AZs, ECS task SG id
- [ ] Reemplazar los `<PLACEHOLDER>` en `infra/elasticache/samconfig.toml` con los valores reales
- [ ] `sam deploy --config-env staging` (o equivalente segun samconfig)
- [ ] Correr `scripts/smoke_test_redis.py` contra el endpoint nuevo, esperar 6/6
- [ ] Aplicar `staging_block` del snippet a la i2c-3 staging task definition y hacer rolling restart
- [ ] Push del branch SH-119 + abrir PR

**Criterio de éxito:** desde un ECS task podés hacer `PING` y obtener `PONG` < 5ms.

**Costo:** ~$15/mes (staging). Production posture queda parametrizada para activar cuando Phase 2 lo justifique.

### 0.4 — Configuración de variables de entorno por backend

Tareas (por backend que migre):
- [ ] Agregar a la task definition: `SUPABASE_MAX_CONNECTIONS`, `SUPABASE_MAX_KEEPALIVE`, `SUPABASE_KEEPALIVE_EXPIRY`, `SUPABASE_TIMEOUT_S`, `SUPABASE_MAX_CONCURRENT_OPS`, `REDIS_URL` si aplica
- [ ] Calibrar valores según compute size de Supabase (ver tabla más abajo)

**Calibrar para Supabase Pro compute micro (2-core ARM, 1GB RAM):**

Estos valores acotan el pool **httpx → PostgREST** del cliente Python (no son conexiones Postgres directas). PostgREST gestiona su propio pool a Postgres internamente — el ceiling efectivo está dado por la capacidad de PostgREST, no por nuestros settings.

| Var | Valor recomendado | Notas |
|---|---|---|
| `SUPABASE_MAX_CONNECTIONS` | 5 | httpx pool size por proceso |
| `SUPABASE_MAX_KEEPALIVE` | 3 | conexiones keepalive |
| `SUPABASE_KEEPALIVE_EXPIRY` | 20 | segundos |
| `SUPABASE_TIMEOUT_S` | 30 | segundos por request |
| `SUPABASE_MAX_CONCURRENT_OPS` | 4 | semáforo asyncio + threading |

**Cuándo upgradear compute:**

| Tier | Capacidad |
|---|---|
| Micro (1GB) | ~20-30 orgs activas con carga normal |
| Small (2GB) | ~80 orgs |
| Medium (4GB) | ~200 orgs |

### Phase 0 — Riesgo: NINGUNO

Esta fase no modifica ningún backend. Si algo falla, es localizado a la infra (Redis no responde, ECS task definition mal config) y se revierte sin afectar datos.

---

## Phase 1 — i2c-3 hardening (riesgo bajo, valor alto) — **DONE (merged 2026-06-26)**

**Objetivo:** estabilizar I2C v3 para producción real con la mayor cobertura de bugs posible. Esta fase entrega valor al cliente sin requerir Pulse architecture.

**Estado al 2026-07-06:**
- [x] **PR #160 (Stability Package)** — merged to `develop`. SH-99, SH-93, SH-100, SH-101, SH-102, SH-103, SH-133, SH-134, SH-135.
- [x] **PR #172** — staging `REDIS_URL` wiring in ECS task template.
- [x] Staging validated: `/ready` → `redis_lua: true`, dedup + golden-path webhook OK.
- [x] **PR #168 (SH-144)** — merged 2026-06-25 (draft backfill estimate + cell-content parity).
- [x] **PR #161 (Beth hotfix)** — merged 2026-06-18, en prod via release 1.6.1 (deployed 2026-06-24, PRs #170/#171 cycle). Seis commits:
  - `1b492cd`: Smartsheet `errorCode 4004` + `shouldRetry` pattern agregados a `_RETRYABLE` (antes solo cubría 502/503/bad gateway/service unavailable/can only concatenate str — faltaban 500 y 4004)
  - `8d6f035`: DLQ retry maneja ambos shapes `attachment_id` (singular) y `attachment_ids` (plural). Pre-fix: cada DLQ entry de row-batch quedaba abandonado con "No attachment_id in event data"
  - `9b30573`: `SmartsheetWebhookService.list_webhooks` reemplazó `includeAll=True` (deprecated, errorCode 1018 spamming 333x/hr) por loop explícito `page=1..N` con `page_size=100`, cap en page 100
  - `d4efd14`: cross-reference annotation contra https://developers.smartsheet.com/api/smartsheet/error-codes — 4004 instruye retry, 4002 cae por substring "500", 4003 (429 rate limit) excluido intencional (SDK maneja backoff), 1004/1018 non-retryable
  - `e96b5ab`: limpieza fleet-wide de `includeAll` en i2c-3 — 4 callsites más en `api/routes/smartsheet.py` (walk_folder, list_worksheets) y `services/smartsheet/sheets.py` (`_list_attachments_by_row`, `get_columns`) cambiados a `pageSize=10000`. Más 3 regression guards en `test_hotfix_2026_06_17_cellimages_dlq.py`
  - `4ddb239`: merge develop → hotfix branch resolviendo conflict de CHANGELOG contra SH-108/SH-121

**Impacto cliente del hotfix:** los 106 DLQ entries atascados de Beth desde el incidente 2026-06-16 auto-recuperados tras 1.6.1 deployed (DLQProcessor 5-min cycle los picked up post-hotfix). Cliente notificado. Más 1 fallo genuino 403/1004 en row `8207567520202628` (file `img_2026-06-16-11-18-17.jpg`) — no es bug de código, requiere acción del lado del cliente (column lock o sheet share scope). Más 5 small-image failures (<50px) — user side.

### 1.1 — Backports targeted (sin esperar smarthub_core)

Aplicar los fixes que cierran bugs sin requerir migración. Ramas independientes, mergean en paralelo a SH-75 o después.

#### 1.1.a — Smartsheet retry expansion (#16, #21)

Tareas:
- [x] Expandir `_RETRYABLE` en `backend/image-to-cell-3/src/services/smartsheet/client.py` con `"500"`, `"internal server error"`, `'"errorcode": 4000'`, `"connectionterminated"`, `"remoteprotocolerror"`, `"server disconnected"` — **+ `errorCode 4004` y `shouldRetry` agregados via PR #161 commit `1b492cd` (2026-06-18, prod 1.6.1)**
- [x] Test: `tests/unit/test_smartsheet_retry.py` que mockea las nuevas excepciones y verifica retry (más regression guards en `test_hotfix_2026_06_17_cellimages_dlq.py`)
- [x] Tamaño real: ~15 líneas + ~10 tests

**Criterio de éxito:** los `errorCode 4000`/`4004` durante backfills dejan de causar row failures. Métrica: `success_rate` por backfill sube de N% a (N+M)%. **Verificado post-1.6.1 sobre el caso Beth.**

#### 1.1.b — `log_image_conversion` internal retry (cierra el subcobro de los 12 errors observados)

Tareas:
- [ ] En `backend/image-to-cell-3/src/services/usage_log.py`, agregar retry 3× con backoff exponencial dentro de `log_image_conversion`
- [ ] Catch only en errores transitorios (`RemoteProtocolError`, `Server disconnected`); fail-fast en errores fatales
- [ ] Test: mock que falla primero N veces y luego succeeds; verifica que el RPC se llamó N+1 veces
- [ ] Tamaño estimado: ~15 líneas

**Criterio de éxito:** los `Failed to log image conversion` warnings desaparecen sustancialmente. Métrica: `count(distinct row_id)` en `imagetocell_usage_log` por backfill = `processed` count del worker.

#### 1.1.c — UNIQUE constraint en `imagetocell_usage_log` (#25 full fix, cierra #27)

Tareas:
- [ ] Migration Supabase: `ALTER TABLE imagetocell_usage_log ADD CONSTRAINT usage_log_unique_per_processing UNIQUE NULLS NOT DISTINCT (organization_id, worksheet_id, row_id, image_column_id, image_filename) WHERE success = true`
- [ ] Update RPC `log_image_conversion`: agregar `ON CONFLICT DO NOTHING`
- [ ] Test que dispara dos llamadas concurrentes con mismos params + verifica solo una entry
- [ ] Coordinación: validar en staging por 1 semana antes de producción
- [ ] Tamaño estimado: 1 migration + ~20 líneas en RPC + 10 tests

**Criterio de éxito:** después de aplicar, escenarios "webhook + backfill race en mismo row" producen 1 entry en usage_log, no 2. Cero double-bill aún si dos paths intentan loggear simultáneamente.

#### 1.1.d — Cross-org webhook zombie deletion fix (#18)

Tareas:
- [ ] Persistir `webhook_id` en `imagetocell_webhooks` al registrar (probablemente ya está; verificar)
- [ ] En `webhook_lifecycle.py:cleanup_zombies`, en lugar de "webhook que no está en mis configs = zombie", evaluar "webhook que no está en NINGUNA config de NINGUNA org del Smartsheet token"
- [ ] Necesita una query global a `imagetocell_webhooks` (todas las orgs) para construir el set de "webhook IDs registrados por nosotros"
- [ ] Test que simula 2 orgs compartiendo Smartsheet token, ejecuta lifecycle alternadamente, verifica que ningún webhook se borra entre ciclos
- [ ] Tamaño estimado: ~50 líneas + tests integración

**Criterio de éxito:** cero ocurrencias de `DISABLED_VERIFICATION_FAILED → re-register → DISABLED_VERIFICATION_FAILED` loop en logs. Métrica: webhooks de cualquier org que comparte token siguen registrados al cabo de 24h.

#### 1.1.e — `discussion` event handling (#20)

Tareas:
- [ ] Cambiar `event_type` y `object_type` en `WebhookEvent` (`src/models/webhook.py`) de strict enum a `str`
- [ ] Mantener las constantes Enum como referencia, pero sin validación estricta en Pydantic
- [ ] En `webhooks.py:webhook_endpoint`, pasar `attachment_events` (la lista pre-filtrada) al `WebhookPayload` constructor — no la lista cruda
- [ ] Test que envía un webhook con eventos mixtos `attachment` + `discussion` y verifica que solo los attachment se procesan, sin crashear
- [ ] Tamaño estimado: ~10 líneas

**Criterio de éxito:** webhooks que llegan junto con comentarios de Smartsheet ya no se descartan. Logs muestran procesamiento normal.

#### 1.1.f — Path traversal fixes (`test_temp_file_manager.py` failing tests)

Tareas:
- [ ] Investigar las 2 fallas pre-existentes en `tests/unit/test_temp_file_manager.py`
- [ ] Corregir `TempFileManager` para bloquear `..\` y `../` en filenames
- [ ] Tests pasan
- [ ] Tamaño estimado: ~20 líneas

**Criterio de éxito:** `test_create_temp_file_path_traversal_blocked` y `test_create_temp_file_path_traversal_windows_style` pasan. Suite i2c-3 al 100%.

#### 1.1.g — DLQ retry shape compatibility (Beth hotfix)

Tareas:
- [x] DLQ retry handler maneja tanto `attachment_id` (singular, legacy webhook events) como `attachment_ids` (plural, row-batch events) — PR #161 commit `8d6f035` (2026-06-18, prod 1.6.1)
- [x] Regression tests en `tests/unit/test_hotfix_2026_06_17_cellimages_dlq.py`

**Criterio de éxito:** entries de DLQ producidos por row-batch attachment events son retentados correctamente. **Verificado**: los 106 entries de Beth se recuperaron solos tras 1.6.1.

#### 1.1.h — Cross-fleet `includeAll` cleanup en i2c-3 (Beth hotfix sequel)

Smartsheet deprecó `includeAll=True` y empezó a devolver `errorCode 1018` (spamming 333x/hr en logs). Reemplazo: pagination explícita.

Tareas:
- [x] `SmartsheetWebhookService.list_webhooks` async → loop `page=1..N` con `page_size=100`, cap en page 100 — PR #161 commit `9b30573`
- [x] 4 callsites adicionales fleet-wide en i2c-3 cambiados a `pageSize=10000`: `api/routes/smartsheet.py` (`walk_folder`, `list_worksheets`) y `services/smartsheet/sheets.py` (`_list_attachments_by_row`, `get_columns`) — PR #161 commit `e96b5ab`
- [x] Cross-reference annotation contra Smartsheet error code docs — commit `d4efd14`
- [ ] **Ticket abierto pendiente** — réplica del barrido fleet-wide a los otros backends (`bulk-attachment-uploader`, `cell-history`, `attachment-counter`, `dynamic-ddl`, `contact_book_connects`). Pre-requisito de Phase 2.

**Criterio de éxito:** logs limpios de `errorCode 1018`. **Verificado en i2c-3 post-1.6.1.**

#### 1.1.i — Backfill estimator: draft config + cell-content parity (SH-144 + SH-145 follow-up)

Tareas:
- [x] `POST /configs/backfill/estimate` acepta `draft_config` opcional (`DraftConfigPayload` pydantic con constraints idénticos a `ConfigCreate`: `image_columns 1-10`, `images_to_process 1-10`, `ConfigType` enum) — PR #168 commit `b7393cf` (merged 2026-06-25)
- [x] Response shape `DraftBackfillEstimate`: `field configs` renombrado a `existing_configs`, nuevo `draft_estimate` field, `totals` consolidados sobre existing + draft
- [x] Endpoint short-circuits solo cuando ambos vacíos
- [x] Helper `_estimate_draft_config` (lines 665+ de `src/api/routes/configs.py`) — materializa `ImageToCellConfig` in-memory bajo sentinel id `draft-preview`, **sin escrituras DB ni cache keys** (regression test mockea todos los Supabase write verbs)
- [x] Cell-content skip fix (SH-145 follow-up, commit `becf79d`): estimator usa el mismo `RowProcessor._extract_cell_images` + `_apply_existing_image_tiers` static methods que el runtime con `is_scan_context=True`. **Estimator y runtime ahora algorítmicamente idénticos** una vez que mode + state + prior_log + cell content están factored in
- [x] Tests: `tests/unit/test_sh144_backfill_estimate_draft_config.py` (21 tests) + `tests/unit/test_sh121_backfill_estimate.py` (11 retro)
- [-] **Frontend SH-122** — AK building contra el contract de SH-144. Happy path local green. Validando ACs (b) overage no-cap, (c) ceiling breach, (d) estimator failure, y toggle "process existing attachments" off. End-to-end staging smoke bloqueado por próxima promoción develop → test (1.6.1 fue cortado pre-merge de SH-144). Plan: deploy frontend + backend juntos fin de sprint.

**Decisiones de producto que esto cementa:**
1. **Name_map T3 semantic mantenido**: cuando un name_map pattern matcha un attachment, runtime procesa aunque la columna target tenga una imagen manualmente subida distinta. Assumption: "user configured esta columna para sync con ese pattern, lo mantenemos en sync". Estimator (post-SH-145) refleja esto fielmente. **Copy del modal SH-122 debe comunicarlo** para que no sorprenda al usuario.
2. **Draft endpoint read-only**: `compute_backfill_estimate` corre sobre `ImageToCellConfig` in-memory armado del draft payload. Cero escrituras a `imagetocell_configs`, cero entries en `usage_log`, cero cache keys bajo sentinel id `draft-preview`.
3. **Estimator/runtime parity**: post-SH-145 son algorítmicamente idénticos. Solo pueden divergir si el sheet state cambia entre preview call y backfill run.
4. **Limitación conocida**: draft name_map sin `namemap_patterns` explícitos retorna 0 porque no populamos `image{N}_column_name` desde el draft payload (solo column IDs). Frontend SH-122 debe requerir al menos un pattern entry. Future option: agregar `image_column_names: Optional[List[str]]` a `DraftConfigPayload`.

**Criterio de éxito:** modal de SH-122 muestra estimate preciso antes de save. Cero side effects del draft path. **Backend criterion: cumplido y mergeado.**

### 1.2 — i2c crypto migration → smarthub_core.crypto

Requiere SH-9 mergeada.

Tareas:
- [ ] Reemplazar `from src.services.crypto import ...` por `from smarthub_core.crypto import ...` en cada call site (~10 imports)
- [ ] Eliminar `backend/image-to-cell-3/src/services/crypto.py`
- [ ] Validar que test suite de i2c-3 sigue pasando
- [ ] Validar que tokens existentes en `smartsheet_tokens` (encriptados con la implementación legacy) decriptan con la nueva
- [ ] Tamaño estimado: ~30 líneas modificadas + 5 imports

**Criterio de éxito:** `pytest backend/image-to-cell-3/tests/` pasa. Webhooks reales en producción procesan tokens decriptados correctamente.

**Riesgo:** mínimo. Wire format está validado byte-a-byte por test cross-module.

### 1.3 — i2c supabase migration → smarthub_core.db.supabase

Requiere SH-9 mergeada + Phase 0.4.

Tareas:
- [ ] Reemplazar `SupabaseService` legacy por `smarthub_core.db.supabase.SupabaseService`
- [ ] Verificar que las llamadas con `execute_with_retry` y `execute_with_retry_async` siguen funcionando
- [ ] Validar que el httpx pool no sobrepasa `SUPABASE_MAX_CONNECTIONS`
- [ ] Configurar las nuevas env vars en la task definition (Phase 0.4)
- [ ] Tamaño estimado: ~50 líneas modificadas

**Criterio de éxito:** durante carga de backfill, observar `httpx pool size` con MONITOR / instrumentación. No debe pasar el límite. `Failed to log image conversion` sigue siendo bajo (con retry interno + pool acotado, casi cero).

**Riesgo:** medio. El semáforo configurable cambia el throughput por proceso. Calibrar en staging.

### 1.4 — i2c smartsheet migration + dedup migration + bolt cap atomic (**Stability Package**)

Después de SH-14 (crypto + Supabase migration, mergeada en PR #139), la migración a `smarthub_core.platforms.smartsheet.SmartsheetClient` no se ejecuta como un único cambio. La investigación de la carga real (BAU dispara cientos de webhooks concurrentes desde múltiples orgs simultáneas) reveló que hay 6 pain points críticos de billing que ni la migración del Smartsheet client ni cualquier sub-paso aislado cierran. Se decidió ejecutarlo como un **Stability Package** completo de 7 tickets que cierra TODOS los pain points y deja i2c-3 en "stable v1" listo para parking.

Plan de ejecución canónico: [`C:\Users\Rene1\.claude\plans\sorted-popping-beaver.md`](file:///C:/Users/Rene1/.claude/plans/sorted-popping-beaver.md) (plan file). Resumen ejecutivo abajo.

#### Decisión de scope: Option A (full async) sobre Option C (partial)

Se evaluaron dos approaches para el Smartsheet client migration:
- **Option C** — migrar solo async-context paths (webhook + dlq), backfill queda con legacy
- **Option A** — full async refactor de toda la stack Smartsheet en i2c-3

Se eligió **Option A** porque BAU + multiple orgs concurrent es exactamente el escenario donde el rate limit cluster-wide debe aplicar en TODOS los paths (incluyendo backfill, que es la operación más costosa por evento). Option C dejaría backfill vulnerable a 429 cascades en el escenario peor caso del cliente.

#### Tickets del package (placeholders SH-XXa..g hasta que se creen en Jira)

| Ticket | Title | SP | Notas |
|---|---|---:|---|
| `SH-XXa` | `[I2C] Add UNIQUE constraint on imagetocell_usage_log + make log_image_conversion idempotent` | 3 | DB safety net. No requiere Redis. Closes #25 full fix + #27 |
| `SH-XXb` | `[Infra] Provision ElastiCache Valkey for staging and production` | 3 | Phase 0.3 ejecutada. CloudFormation. Paralelo con SH-XXa |
| `SH-XXc` | `[I2C] Migrate webhook dedup caches to Redis (cross-process safety)` | 5 | Cross-task dedup. Closes #26 + parte de #27 |
| `SH-XXd` | `[I2C] Migrate Smartsheet wrappers (sheets/attachments/webhooks) to async smarthub_core client` | 5 | Sub-PR 1 de Option A. Closes #16 + #21 + #17 en los wrappers |
| `SH-XXe` | `[I2C] Migrate i2c-3 core processor and row_processor to async-first` | 5 | Sub-PR 2. Drop ThreadPoolExecutor en core. Cluster-wide rate limit aplica a backfill |
| `SH-XXf` | `[I2C] Drop ThreadPoolExecutor from webhook_handler and dlq_processor` | 3 | Sub-PR 3. Unifica modelo de concurrencia en async |
| `SH-XXg` | `[I2C] Distributed bolt cap atomic deduct via Redis Lua` | 3 | Cierra TOCTOU del bolt cap. Closes overshoot bajo BAU burst |

**Total: 27 SP, ~10-12 semanas calendar.** Cada ticket ≤ 5 SP (bajo el límite del equipo de 8 SP).

#### Deployment milestones — versión funcional incremental

El sistema entra en estado deployable después de cada ticket merged. Si una prioridad mayor surge, podríamos detenernos en cualquier milestone con el sistema más robusto que antes:

| Milestone | Tras qué tickets | Pain points cerrados | Estado del sistema |
|---|---|---|---|
| **M1: DB safety net** | `SH-XXa` | #1, parte de #2 | Double-bill via webhook retry cross-task cerrado a nivel DB |
| **M2: Cross-task dedup robusto** | `SH-XXa..c` | #1, #2 completo, parte de #3 | Dedup sobrevive ECS restart y load-balance |
| **M3: Smartsheet wrappers async** | `SH-XXa..d` | + #5 (rate limit), #6 (retry gaps) | Cluster-wide rate limit + retry en wrappers |
| **M4: Core async-first** | `SH-XXa..e` | Mismos que M3 + memoria menor | Backfill async-first, ~10% menos memoria |
| **M5: ThreadPoolExecutor eliminado** | `SH-XXa..f` | Mismos que M4 | Modelo concurrencia unificado (async en todo) |
| **M6: Stable v1** | `SH-XXa..g` | **TODOS los 6** | Bolt cap atomic. i2c-3 "parked" listo para otras tools |

#### Sequence

```
1. SH-XXa  UNIQUE constraint              ──┐
                                              │ paralelo
2. SH-XXb  ElastiCache provisioning        ──┘
   ▼
3. SH-XXc  Redis dedup                   → M2
   ▼
4. SH-XXd  Wrappers async                → M3
   ▼
5. SH-XXe  Core async                    → M4
   ▼
6. SH-XXf  Drop ThreadPoolExecutor       → M5
   ▼
7. SH-XXg  Bolt cap atomic               → M6 (Stable v1)
```

**Criterio de éxito (post-`SH-XXg`):**
- 0 duplicates en `imagetocell_usage_log` durante 7 días post-deploy
- count(http 429) Smartsheet < 0.5%
- 0 bolt cap overshoot por org
- BAU desde 2 orgs simultáneas (500 attachments cada uno) → 0 errores, 0 double-bills

**Riesgo del package:** medio-alto en SH-XXe (core async refactor del hot path de billing). Mitigaciones:
- SH-XXa deployado antes de SH-XXe → DB rechaza duplicates aunque el código tenga bugs
- SH-XXc deployado antes de SH-XXe → cross-process dedup robusto
- Cada PR independientemente revertible (splits están diseñados para rollback aislado)
- Mínimo 1 semana en staging entre cada milestone antes de prod

### Phase 1 — Risk note for current sprint (2026-06-25)

**El rebase de PR #160 sobre develop post-sprint es la operación más riesgosa pendiente.** PR #160 está abierto hace varias semanas y desde entonces mergearon:
- PR #161 (Beth hotfix) — reescribe pieces del Smartsheet client que SH-100 (slice 3 de Phase 1) ya había reestructurado a async. El fix sync de `list_webhooks` colisiona con la nueva signature async.
- PR #168 (SH-144) — renombra `configs` → `existing_configs` en el response shape de `compute_backfill_estimate`. Cualquier test o caller en Phase 1 que asuma el shape viejo necesita actualizarse durante el rebase.
- PR #165 (SH-123 + SH-140) — attachment-counter heartbeat. No toca i2c-3 directamente pero está en el mismo develop history.

**Plan de rebase:**
1. Rebase PR #160 sobre `develop` con conflict resolution explícita en `services/smartsheet/webhook.py` y `api/routes/configs.py`
2. Re-correr suite completa de i2c-3 + regression guards de Beth hotfix
3. Smoke contra staging post-rebase, antes de mergear
4. Mergear con squash, deploy a staging por mínimo 1 semana antes de prod

### Phase 1 — Off-ramp criteria

**Antes de iniciar Phase 2 stream cutover (SH-147 gate), verificar:**

- [x] i2c-3 con `smarthub_core` en prod (SH-14/PR #139; 1.6.1+ Phase 1 in staging)
- [ ] Suite i2c-3 al 100% (incluyendo path traversal fixes — 2 pre-existing failures)
- [ ] `Failed to log image conversion` < 0.1% de uploads
- [x] No incidents billing abiertos — Beth 2026-06-16 cerrado
- [ ] Cliente sin issues de webhook latency
- [x] PR #160 mergeado; **~1 semana estable en staging** (SH-147) antes de prod Phase 2
- [ ] Cross-fleet `includeAll` cleanup (1.1.h) en otros backends — no bloquea I2C lean path

Si Phase 2 streams no tiene métricas que lo justifiquen → **detenerse tras SH-148/150** (locks only). Gateway + streams requieren trigger criteria de Phase 2.

---

## Phase 2 — I2C hardening + stream-ready runtime (LEAN, semanas 3–8)

**Objetivo:** cerrar gaps de concurrencia restantes en I2C, preparar el runtime para Redis Streams **sin duplicar ECS services**. Epic Jira: **SH-146**.

**Estado al 2026-07-06:**
- Phase 1 (PR #160) merged; staging Redis validated.
- SH-148 / SH-150 code complete (PRs #173, #174 pending merge).
- SH-151 reframed: runtime flags, not forced `APP_MODE` split.
- SH-154 / SH-157 deferred (dual ECS).

### Phase 2 — Trigger criteria (sin cambio)

Iniciar streams + gateway cutover **solo si AL MENOS UNA** es cierta (las locks SH-148/150 no dependen de esto):

- Webhook receive latency p95 > 4s sostenido por >1 semana en producción
- Múltiples incidents/mes de "FastAPI process killed mid-BackgroundTask"
- Cliente reportando webhook reliability issues
- Backfills que tardan >15 min están afectando UX

Multi-réplica (`desiredCount > 1`) es **opcional** hasta que escala horizontal sea necesaria — no gate del plan lean.

### 2.1 — Migrar caches in-memory de I2C a Redis

> **Status 2026-07-06:** **Done in Phase 1 (PR #160)** — SH-99 (dedup), SH-93 (token + rate limit), SH-103/135 (bolt-cap). Validate in staging after deploy. Do not re-implement.

Tareas originales (referencia histórica):
- [x] Reemplazar `_processing_cache` por `dedup:i2c:processing:{att_id}` con TTL 600s
- [x] Reemplazar `_row_cache` por `dedup:i2c:row:{cfg}:{row}` con TTL 600s
- [x] Reemplazar `_completed_attachments` por `dedup:i2c:completed:{att_id}` con TTL 600s
- [x] Granularidad per-attachment dedup
- [x] Fallback in-memory por proceso para Redis-down

**Criterio de éxito:** dos workers procesando el mismo attachment — solo uno hace upload. **Validado en staging Phase 1.**

### 2.2 — Bolt cap context cache compartido

> **Status 2026-07-06:** Cache Redis + atomic deduct in Phase 1. **SH-150** adds stampede lock on cache miss (PR #174).

- [x] `_usage_cache` → Redis `bolt-cap:{org_id}` TTL 30s (Phase 1)
- [x] Stampede prevention `lock:fetch:bolt-cap:{org}` — **SH-150**

**Criterio de éxito:** cluster-wide overshoot < 1 imagen/min bajo carga concurrente.

### 2.3 — Distributed lock para concurrent backfills (#26)

> **Status 2026-07-06:** **SH-148 done** — PR #173. Worksheet-scoped lock + heartbeat.

- [x] `acquire_lock` en `backfill_worksheet` con token + heartbeat 60s
- [x] Concurrent call → `backfill_already_running`
- [x] Tests: `tests/unit/test_sh148_backfill_lock.py`

**Criterio de éxito:** doble click en backfill → uno procesa, otro rechaza. **Merge #173.**

### 2.4 — Runtime role flags (replaces forced APP_MODE split)

> **Replaces MIGRATION_PLAN 2.4 original** ("Worker mode separation" con dual ECS). SH-151 reframed.

**Principio:** el código soporta roles separados, pero **deploy default = monolito**.

Tareas:
- [ ] Env flags: `RUN_HTTP_API` (default `true`), `RUN_STREAM_CONSUMER` (default `true`), `RUN_LIFECYCLE_POLLER` (default `true` until gateway owns lifecycle)
- [ ] Optional legacy alias: `APP_MODE=all|api|worker-i2c` mapping to flags — `all` = both HTTP + consumer
- [ ] En `app.py` lifespan: start uvicorn routes when `RUN_HTTP_API`; start `SmarthubWorker.run()` background task when `RUN_STREAM_CONSUMER`
- [ ] **No new ECS service** for worker-only mode in staging/prod until metrics demand it
- [ ] Tests: monolith mode starts both; `api`-only skips consumer; `worker`-only skips HTTP (for future scale-out)

**Criterio de éxito:** una sola task ECS ejecuta API + stream consumer sin duplicar trabajo. Split por env var disponible pero no default.

### 2.5 — Stream publisher + consumer (monolithic)

> Maps to **SH-153** (publish) + **SH-152** (consume). Consumer runs **embedded** in I2C service, not separate ECS.

**SH-153 — Publisher (gateway preferred):**
- [ ] Gateway receives webhook → validate HMAC → `XADD stream:i2c` with versioned envelope
- [ ] Transitional: I2C API can also publish (shadow mode) before gateway cutover
- [ ] Shadow mode: publish to stream **and** process inline; compare logs/metrics

**SH-152 — Consumer (embedded):**
- [ ] `SmarthubWorker` consumes `stream:i2c` via `XREADGROUP` in same process as HTTP API
- [ ] Reuse existing `webhook_handler` processing logic; dedup keys unchanged
- [ ] DLQ: existing Supabase `imagetocell_dlq` + optional `stream:i2c:dlq` (SH-155)

**Cutover sequence:**
1. Shadow: gateway publishes, I2C still processes direct path
2. Dual receive: both paths active, compare outcomes
3. URL cutover to gateway
4. Disable inline webhook processing in I2C (`RUN_WEBHOOK_RECEIVER=false`)

**Criterio de éxito:** webhook burst → events in `stream:i2c` → monolithic I2C consumer → images in sheet. p95 receive < 200ms at gateway.

### 2.6 — Dual ECS services (DEFERRED)

> Original SH-154. **Not in lean path.** Revisit only when:
> - API p95 latency and consumer lag need independent scaling, OR
> - CPU-bound processing saturates HTTP health checks on shared tasks

If triggered later:
- Deploy second ECS service with `RUN_HTTP_API=false`, `RUN_STREAM_CONSUMER=true`
- Same container image, different env vars — no code fork

### 2.7 — platform-events + stream DLQ (optional, SH-155)

- [ ] On fatal row failure: `XADD stream:i2c:dlq`
- [ ] On success: `XADD stream:platform-events` with `type=image_processed`
- [ ] No consumer required yet — events accumulate for Phase 5 / cross-tool workflows
- [ ] **Priority:** after 2.5 stable, not blocking gateway or cutover

### Phase 2 — Off-ramp criteria (revised)

**Before starting Gateway URL cutover (Phase 3.4) and polling-tool migration (Phase 4):**

- [ ] SH-148 + SH-150 merged and deployed to staging
- [ ] SH-147 gate: Phase 1 stable ~1 week in staging/prod
- [ ] SH-151 runtime flags merged; monolith default verified
- [ ] SH-152/153 stream path validated in shadow mode on staging

**Before dual ECS (2.6) — only if needed:**

- [ ] Multi-replica smoke (SH-149) passed
- [ ] Independent scaling justification documented (metrics)

If Phase 2 closes operational pain points with monolith + gateway: **stop here** until polling migration or cross-tool decoupling is customer-driven.

---

## Phase 3 — Webhook Gateway (polyglot desde día 1, semanas 5–10)

**Objetivo:** un solo punto de entrada para webhooks. En el plan lean, el gateway se construye **en paralelo con 2.4–2.5** (no después de dual ECS).

> **Lean note:** Phase 3 gateway + Phase 2.5 streams are one continuous track. Gateway is +1 shared ECS service; tools keep 1 service each with embedded consumers.

### Phase 3 — Trigger criteria

Iniciar **solo si AL MENOS UNA** es cierta:

- Es el segundo backend que necesita gateway centralized (i2c-3 + uno más)
- Webhook receive latency p95 > 6s en cualquier backend
- Operaciones manuales de webhook URL migration son frecuentes y costosas
- Hay un segundo provider entrando (HubSpot, Slack, etc.) — desde el día que se agregue el segundo provider, el gateway centralized vale más que cada backend manejando su provider individualmente
- Cross-provider workflows: tool A reacciona a evento de tool B (requiere `stream:platform-events` corriendo)

Si solo necesitás centralizar el lifecycle manager pero los receivers individuales funcionan bien y solo hay un provider, **considerá una versión austera**: lifecycle manager separado pero cada backend mantiene su receiver. Ahorra 60% del trabajo.

### 3.1 — Build webhook gateway service (polyglot)

Tareas:
- [ ] Crear `services/webhook-gateway/` como nuevo servicio FastAPI deployable
- [ ] **Routing polyglot:** `POST /webhook/{provider}/{tool_slug}/{org_id}` — el path segment `{provider}` selecciona el handler. Hoy: `smartsheet`. Mañana: `hubspot`, `slack`, `stripe`, `twilio`.
- [ ] **Handler registry** (`gateway/routing.py`): registry dispatch por `provider` que delega a `gateway/handlers/{provider}.py`. Cada handler hereda de `smarthub_core.platforms.shared.webhook_base.BaseWebhookHandler` con métodos `verify_signature(headers, body) -> bool`, `handle_challenge(body) -> Response | None`, `extract_event(body) -> ParsedEvent`, `normalize_envelope(event) -> dict`.
- [ ] **Initial handlers:**
  - `gateway/handlers/smartsheet.py` — HMAC SHA-256 verification + sync challenge response (Smartsheet 5s SLA)
- [ ] Lookup config en cache `webhook-secrets:{org}:{slug}` con cache-aside (ver [TARGET_ARCHITECTURE.md#caching-strategy](TARGET_ARCHITECTURE.md#caching-strategy) clave #1; el value es JSON map `{webhook_id: encrypted_secret}` porque hay un secret por sheet/webhook)
- [ ] Encolar evento parsado en `stream:{tool_slug}` con [envelope versionado](TARGET_ARCHITECTURE.md#stream-payload-envelope-versionado) (incluye `provider`, `event_type`, `event_id`, `received_at`, `raw`)
- [ ] 2 replicas mínimo (HA)
- [ ] Tamaño estimado: ~600 líneas (gateway + smartsheet handler) + ECS service + 80 tests

**Criterio de éxito:** un webhook Smartsheet hace el round-trip (HMAC validate + cache lookup + XADD + 200 OK) en < 100ms p95. La estructura del routing permite agregar un handler de HubSpot con cero modificación al core del gateway.

### 3.2 — Lifecycle Manager unificado con Redis lock

Tareas:
- [ ] Mover lógica de `webhook_lifecycle.py` de I2C al gateway
- [ ] Singleton via `lock:lifecycle:manager` con TTL 600s + heartbeat 60s (ver [TARGET_ARCHITECTURE.md#lifecycle-manager-redis-lock](TARGET_ARCHITECTURE.md#lifecycle-manager-redis-lock) — Lua release token-checked)
- [ ] Si más de 1 replica del gateway: solo el holder del lock corre el ciclo
- [ ] Construir webhook URL targets a la nueva URL del gateway con el path segment `{provider}` correcto
- [ ] **Provider-agnostic:** el lifecycle manager opera sobre un `WebhookLifecycle` interface que cada provider implementa (`smartsheet.WebhookLifecycle`, `hubspot.WebhookLifecycle`, etc.). Registrar webhook, renovar, listar zombies, eliminar — la lógica del manager itera providers y delega.
- [ ] Cleanup de zombie webhooks (#18 fix de Phase 1.1.d) ahora vive aquí cross-tool y cross-provider
- [ ] Tamaño estimado: ~400 líneas + tests

**Criterio de éxito:** lifecycle manager ejecuta cycles sin overlap incluso con multi-replica. Agregar un nuevo provider = agregar un `WebhookLifecycle` impl, no tocar el manager.

### 3.3 — Platform-events bus operacional (essential a multi-provider)

A multi-provider esto **deja de ser opcional** — sin bus no hay forma limpia de que un tool de un provider reaccione a eventos de otro tool de otro provider.

Tareas:
- [ ] Stream `stream:platform-events` declarado y consumer groups creados
- [ ] Workers de Phase 2.7 / SH-155 publican `image_processed` aquí — verificar que el envelope incluye `provider`, `tool`, `event_type`
- [ ] Documentar el contrato de tipos de evento que cualquier worker puede publicar/consumir (`image_processed`, `row_added`, `contact_created`, `payment_succeeded`, etc.) en `docs/architecture/PLATFORM_EVENTS.md` (a crear)
- [ ] Schema versioning: `v=1` inicial; cualquier breaking change publica `v=2` y mantiene v=1 simultáneo durante deprecation window
- [ ] Tamaño estimado: ~50 líneas + documento

**Criterio de éxito:** `stream:platform-events` con tráfico real, al menos un consumer subscribed, ningún workflow cross-provider haciendo polling o llamadas directas entre tools.

### 3.4 — Migration de webhook URLs por provider

**La parte más delicada.** Cada org en producción tiene webhooks registrados a URLs viejas. Hay que migrar a la URL del gateway, **un provider a la vez**.

Tareas (ejemplo para Smartsheet; repetir cuando se agregue cada provider):
- [ ] Script de migration que itera todos los webhooks Smartsheet activos en `imagetocell_webhooks`, `dynamic_ddl_webhooks`, etc., re-registra cada uno con la nueva URL `/webhook/smartsheet/{tool}/{org}`, valida challenge OK, marca old webhook como migrado
- [ ] Ventana de cutover por (provider, tool) — no migrar todos los providers al mismo tiempo
- [ ] Monitoring: queue depth de `stream:{tool}` debe arrancar > 0 inmediatamente si hay tráfico de webhooks
- [ ] Rollback plan: revertir a URL vieja si latencia o error rate sube
- [ ] Tamaño estimado: 1 script per provider + procedimiento de cutover documentado

**Criterio de éxito:** 100% de webhooks migrados sin pérdida de eventos durante el cutover.

**Riesgo:** alto. Si la migration se rompe, hay un período donde el provider manda webhooks a URLs viejas que ya no procesan.

### 3.5 — i2c stream consumer (monolithic — revised)

> **Lean change:** I2C does **not** become worker-only. It keeps HTTP API (configs, backfill, health) and **embeds** the stream consumer in the same process.

Tareas:
- [ ] Disable direct webhook receiver in I2C when gateway owns ingress (`RUN_WEBHOOK_RECEIVER=false`)
- [ ] Embedded consumer (`RUN_STREAM_CONSUMER=true`) processes `stream:i2c` via `SmarthubWorker.run()`
- [ ] Filter `envelope.provider == "smartsheet"`
- [ ] Lifecycle manager moves to gateway (3.2); remove from I2C when cutover complete
- [ ] Optional future: split to worker-only ECS if 2.6 triggered

**Criterio de éxito:** i2c-3 = HTTP API + stream consumer in one service. Webhook ingress only via gateway. No duplicate processing.

### Phase 3 — Off-ramp criteria (revised)

**Before starting Phase 4 (polling → webhook), verify:**

- [ ] Gateway in staging/production processing I2C webhooks for ≥2 weeks
- [ ] Webhook receive p95 < 200ms at gateway
- [ ] Zero event loss (stream depth + DLQ correlation)
- [ ] SH-156 E2E signed off: **1 I2C service + gateway**, not dual ECS
- [ ] Rollback drill documented (revert URL to I2C direct receiver)

---

## Phase 4 — Eliminar polling per-tool (CONDITIONAL, secuencial por tool)

**Objetivo:** reemplazar polling con webhooks event-driven cuando hay valor de cliente. **Mismo patrón lean:** gateway → stream → monolithic consumer + reconciliation poll (10–15 min).

### Phase 4 — Trigger criteria

**Por tool** (not all-or-nothing):

- **Attachment Counter (Attachment Info):** cliente reporta latencia de conteo; full-sheet scans costosos — **first priority**
- **Cell History:** cliente reporta latencia de detección de cambios — second (more complex: cell history API per change)
- **Dynamic DDL:** polling 90s acceptable — **do not migrate**

If current polling causes no complaints, **do not migrate**. Webhooks can miss events during blips; reconciliation poll is the safety net.

### 4.1 — Attachment Counter → webhooks (FIRST — 1–2 weeks)

Tareas:
- [ ] Gateway: register Smartsheet webhooks for Attachment Info sheet configs
- [ ] Embedded stream consumer in `attachment-counter` service (`stream:attachment-counter`)
- [ ] On webhook: update counter for changed row only (not full sheet scan)
- [ ] Reconciliation poll every 10–15 min as fallback
- [ ] Migrate `crypto_helper.py` → `smarthub_core.crypto`

**Pattern (reusable for 4.2):**
```
Smartsheet → Gateway → stream:{tool}
                              ↓
                    {tool} monolith (API + consumer)
                    + slow reconciliation poll
```

### 4.2 — Cell History → webhooks (SECOND — 1.5–2.5 weeks)

Tareas:
- [ ] Same gateway + stream + monolith pattern as 4.1
- [ ] On webhook: read only changed cells/rows; call Smartsheet cell history API per change
- [ ] Careful loop prevention (writes to same sheet — Change-Agent header)
- [ ] Reconciliation poll 10–15 min fallback
- [ ] Migrate `crypto_helper.py` → `smarthub_core.crypto`

### Out of scope for Phase 4

- **Dynamic DDL**: polling 90s acceptable. Do not migrate.
- **TextNow**: manual webhook registration, outside gateway. Do not migrate.

---

## Phase 5 — Cross-tool decoupling (CONDITIONAL, semana variable)

**Objetivo:** Bulk Uploader desacoplado de I2C via platform-events bus.

### Phase 5 — Trigger criteria

- Bulk Uploader rompe cuando i2c está caído (dependencia implícita actual)
- Hay un segundo consumer hipotético de eventos de i2c (no solo Bulk Uploader)
- Stream `stream:platform-events` ya tiene tráfico (Phase 2.7 / SH-155)

### 5.1 — Bulk Uploader como consumer de stream:platform-events

Tareas:
- [ ] Eliminar dependencia implícita de webhooks de I2C
- [ ] Implementar `SmarthubWorker` que consume `stream:platform-events` filtrando `type="image_processed"`
- [ ] Migrar `crypto_helper.py` → `smarthub_core.crypto`

**Criterio de éxito:** Bulk Uploader funciona aunque i2c esté caído. Los eventos quedan en stream y se procesan cuando i2c se recupera.

---

## Phase 6 — Observability (PARALELO, anytime)

**Objetivo:** visibilidad completa del sistema en producción.

**No depende de ninguna fase.** Se puede empezar antes de Phase 1 y se aplica incrementalmente.

### Tareas

- [ ] **Logging estructurado JSON en todos los workers**: campos `tool`, `org_id`, `job_id`, `duration_ms`, `correlation_id`. Prerequisite para tracing.
- [ ] **Métricas en Redis**: `metrics:queue_depth:{tool}`, `metrics:jobs_per_min:{tool}`, `metrics:latency_p50/p95:{tool}`. Workers exportan periódicamente.
- [ ] **Dashboard**: Grafana o Railway. Paneles por tool: queue depth, error rate, processing time, billing métrics.
- [ ] **Alertas**:
  - Cola > 500 jobs → Slack/email
  - DLQ > 10 jobs → Slack/email
  - Gateway 5xx rate > 1% → PagerDuty
  - Lifecycle manager sin ciclo en > 10 min → alerta
  - Bolt cap overshoot per org > 1% → alerta de billing
- [ ] **Health endpoint estandarizado**: cada worker expone `GET /health` via `smarthub_core.worker.health.compute_health_report`. ECS health check + dashboard de observabilidad lo consumen.
- [ ] **Correlation ID por webhook event**: generado en gateway, propagado a worker, presente en todos los logs del job.

### Criterio de éxito

Cuando un cliente reporta un issue, podés trazar el evento desde el webhook recibido hasta el cobro en `usage_log` con un correlation ID y línea de tiempo clara.

---

## Orden de migración de `crypto_helper.py`

Una vez `smarthub_core.crypto` esté listo (Phase 0.2), migrar en este orden:

1. **I2C** (Phase 1.2) — ya familiar, menor riesgo
2. **Cell History** (Phase 4.1)
3. **Attachment Counter** (Phase 4.2)
4. **Bulk Uploader** (Phase 5.1)
5. **id-checker** — coordinar con frontend (riesgo medio)
6. **encrypt-tool** — deprecar el microservicio cuando todos usen la librería

> **TextNow y Dynamic DDL fuera de scope** — `crypto_helper.py` permanece divergido en esos backends. Aceptable mientras no se modifiquen frecuentemente.

**NUNCA migrar más de un backend por deploy.** Verificar en staging antes de producción.

---

## Rollback por fase

| Fase | Rollback |
|---|---|
| 0.1 ~~PgBouncer~~ | N/A — fase descartada el 2026-05-06 (todos los backends usan supabase-py REST, no Postgres directo) |
| 0.2.a smarthub_core restructure | `git revert` del commit de restructure. Nada consume `smarthub_core.platforms.*` todavía. Tests siguen pasando con paths viejos. Cero impacto. |
| 0.2.b smarthub_core delivery | Eliminar el paquete del repo. Nada se rompe (no se usa todavía) |
| 0.3 ElastiCache | Decommissionar. Sin datos transaccionales |
| 1.1.x backports | Revert del commit. Backend vuelve al comportamiento anterior |
| 1.2 i2c crypto | Re-incluir `src/services/crypto.py`, revertir imports. Tokens decifran igual |
| 1.3 i2c supabase | Revertir env vars + import del client legacy |
| 1.4 i2c smartsheet | Re-importar `SmartsheetService` legacy. **Más invasivo.** Requiere cuidadoso rollback. |
| 2.x lean (locks + runtime flags + embedded consumer) | Revert env flags; disable stream consumer; Redis state reset. No transactional data loss. |
| 2.6 dual ECS (if ever deployed) | Scale worker service to 0; single monolith service with both flags true |
| 3.1 gateway service | Deploy gateway with feature flag OFF; webhooks keep going to direct backends until cutover. If issues after cutover, revert URLs |
| 3.2 lifecycle manager | Re-enable lifecycle manager inside each backend; disable gateway singleton |
| 3.3 platform-events bus | Stream inert if no consumer; disable subscriber and revert to direct coupling if needed |
| 3.4 URL migration | Re-register webhooks with legacy backend URL; reverse cutover per provider |
| 3.5 i2c stream consumer | Re-enable direct webhook receiver; disable embedded consumer. Gateway can stay up with feature flag OFF |
| 4.x polling → webhook | Reactivar polling, deshabilitar webhook subscription |
| 5 Bulk Uploader | Revertir a dependencia implícita de I2C |
| 6 Observability | Deshabilitar alertas, no afecta funcionamiento |
| Nuevo provider (post-Phase 3) | Eliminar tabla `{provider}_tokens`, eliminar handler del gateway (`gateway/handlers/{provider}.py`), eliminar módulo `smarthub_core.platforms.{provider}`. El core no se toca. |

---

## Estimaciones de tiempo (calibradas)

| Phase | Estimación optimista | Estimación realista | Confidence |
|---|---|---|---|
| 0.1 ~~PgBouncer~~ | N/A | N/A | — (descartada 2026-05-06) |
| 0.2.a smarthub_core restructure | DONE | DONE 2026-05-07 (commit 7648e31) | — |
| 0.2.b smarthub_core delivery | DONE | DONE — merged via PR #131 (2026-05-13) | — |
| 0.3 ElastiCache | 1 día | code DONE locally on SH-119 (`ecef105`); deploy bloqueado por SSO setup del usuario | Media |
| 0.4 ECS task defs | 1 día | 2 días por backend | Alta |
| 1.1.a-c, 1.1.e, 1.1.g-i backports | DONE | merged via PRs #161, #168 (2026-06-18 / 2026-06-25) | — |
| 1.1.d webhook zombie + 1.1.f path traversal | 1 semana | 1.5-2 semanas | Alta |
| 1.2 i2c crypto | DONE | DONE via SH-14 / PR #139 (2026-05-13) | — |
| 1.3 i2c supabase | DONE | DONE via SH-14 / PR #139 (2026-05-13) | — |
| 1.4 Stability Package (PR #160) | 3 días | 10-12 semanas — in flight, rebase pending | Baja |
| 2.x I2C hardening + stream-ready (lean) | 1 semana | 2–3 semanas (locks merged; streams + gateway in flight) | Media |
| 2.6 dual ECS (deferred) | — | Only if metrics trigger | — |
| 3.x gateway polyglot (Smartsheet v1 lean) | 1.5 semanas | 3–4 semanas | Media |
| 4.1 attachment-counter webhook | 1 semana | 1–2 semanas | Media |
| 4.2 cell-history webhook | 1.5 semanas | 2–2.5 semanas | Baja |
| 5 Bulk Uploader | 1 semana | 1.5-2 semanas | Media |
| 6 Observability | continuo | continuo | — |
| Nuevo provider (post-Phase 3) | 2 semanas | 3-4 semanas (variable según complejidad de auth/webhook) | Baja |

**Las estimaciones "1 semana" del plan original eran optimistas en la mayoría de los casos.** Calibrar según equipo size y experiencia con la infra.

---

## Decisión: ¿Pulse lean, full split, o austere?

Three paths — re-evaluate at each phase off-ramp:

### Austere (stop after Phase 1)

- I2C stable with Redis dedup, rate limit, bolt cap
- No gateway, no streams, no polling migration
- **When:** < 5 new orgs/month, no webhook latency pain, single replica sufficient

### Pulse lean (recommended path — 2026-07-06)

- **+1 shared gateway ECS** + **1 service per tool** with embedded stream consumers
- Redis Streams decouple receive from process; code ready for split later via env flags
- Polling tools migrate in ROI order (Attachment Info → Cell History)
- **Cost:** ~+$15–45/mo gateway + existing ElastiCache
- **Duration:** ~6–10 weeks after SH-148/150 merge
- **When:** webhook reliability matters, preparing for 5–8 tools/year, want stream path without infra duplication

### Pulse full split (deferred)

- Separate `api` + `worker` ECS per tool (original SH-154 plan)
- **When:** independent scaling of HTTP vs CPU-bound consumer proven by metrics (lag, p95, OOM on shared tasks)
- **Cost:** +N ECS services (2× per tool that splits)

**Key metric for lean vs full split:** consumer lag p95 > 30s while API health checks fail on same task → trigger 2.6 dual ECS for that tool only.

**Org growth metric (lean vs austere):**
- < 5 orgs/mo new → austere still valid
- > 10 orgs/mo new → lean path justified

This decision is **not one-shot**. Each phase has off-ramp criteria. SH-154 and SH-157 remain in backlog as `deferred-scale-out`.
