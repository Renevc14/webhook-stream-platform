# Tool Registry — Smarthub

Registro central de todas las herramientas. Actualizar cuando se agregue o modifique una herramienta.

---

## Herramientas activas

### image-to-cell-3

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/image-to-cell-3/` |
| **Puerto** | 3011 |
| **Product slug** | `smart-image-to-cell` |
| **Stack** | FastAPI + Pillow + PyMuPDF |
| **Modo** | Webhooks via **Webhook Gateway** + `stream:i2c` (staging Pulse cutover) |
| **Frontend env** | `REACT_APP_I2C_BACKEND_URL` |
| **Processing mode (prod)** | `webhook` |
| **Processing mode (test)** | `webhook_v3` |

**Tablas propias:**
- `imagetocell_config` — configuraciones de usuario
- `imagetocell_webhooks` — **mirror soft-fail** de registros Smartsheet (write SoT = `platform_webhook_registrations`)
- `imagetocell_dlq` — dead letter queue
- `imagetocell_usage_log` — billing trail; **staging (2026-08-03):** unique-per-processing indexes dropped (`20260803190000` / applied `20260803231907`) so reprocess after clear can insert another success row (charge-on-process). Non-unique `imagetocell_usage_log_prior_lookup_idx` remains. Webhook-retry double-bill protection is app-level (SH-99 / stream dedup), not UNIQUE.
- `imagetocell_row_state` — estado por fila

**Tablas compartidas / plataforma:**
- `platform_webhook_registrations` — write SoT de webhooks (provider/tool/org/sheet)
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`, `organization_subscriptions`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY, REDIS_URL
SMARTSHEET_WEBHOOK_CALLBACK_BASE_URL   # gateway public base
GATEWAY_INTERNAL_URL, GATEWAY_M2M_TOKEN  # ensure-registration + secret push (cutover)
RUN_HTTP_API, RUN_STREAM_CONSUMER
RUN_LIFECYCLE_POLLER=false               # staging/prod cutover
WEBHOOK_LIFECYCLE_ENABLED=false
RUN_WEBHOOK_RECEIVER=false
WEBHOOK_PROCESSING_MODES                 # default: "webhook"
ENCRYPT_TOOL_INTERNAL_URL, SMARTSHEET_BROKER_SECRET
```

**Dependencias de otras herramientas:**
- Ingress: `services/webhook-gateway` → `stream:i2c`
- Billing: `smarthub_core.billing` / `BillingPort` (shared Redis `bolt-cap:{org}:*` with SmartQR); charge every successful process; skips + SH-111 shuffle free
- Staging ECS pin: `stg-image-to-cell:26` (`d824e30`) — cutover flags as above
- Publica eventos a `stream:platform-events` (SH-155) para consumidores futuros

---

### bulk-attachment-uploader

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/bulk-attachment-uploader/` |
| **Puerto** | 3003 |
| **Stack** | FastAPI + Redis (estado distribuido) |
| **Modo** | REST API + background tasks |
| **Frontend env** | `REACT_APP_BAU_BACKEND_URL` |

**Tablas propias:**
- `batr_upload_sessions`
- `batr_column_auto_config`
- `batr_usage_log`
- `v_batr_organization_usage_summary` (view)
- `v_batr_user_usage_summary` (view)

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY
REDIS_URL   # para estado distribuido
```

**Dependencias de otras herramientas:**
- Dependencia implícita de I2C: usa los webhooks que I2C registra en Smartsheet
- Migración planeada: consumir `stream:platform-events` en vez de depender de I2C

---

### cell-history

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/cell-history/` |
| **Puerto** | 3014 |
| **Product slug** | `single-cell-audit` |
| **Stack** | FastAPI (API) + background poller (main_app.py) |
| **Modo** | Polling 30 segundos |
| **Frontend env** | `REACT_APP_CH_BACKEND_URL` |
| **Concurrencia** | 15 configs simultáneas |

**Tablas propias:**
- `cell_history_configurations`
- `cell_history_audit_log`
- `cell_history_metrics_*` (múltiples tablas de métricas)

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`, `organization_subscriptions`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY
CH_POLL_INTERVAL_SEC       # default: 30
CH_MAX_CONCURRENT_CONFIGS  # default: 15
CELL_HISTORY_PORT          # default: 3014
```

**Deuda técnica:**
- Polling en 30s — candidato a migrar a webhooks (Fase 3)

---

### attachment-counter

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/attachment-counter/` |
| **Puerto** | 3005 (API: 3005, poller: 3006) |
| **Stack** | FastAPI (API) + background poller (main_app.py) |
| **Modo** | Polling 70 segundos |
| **Frontend env** | `REACT_APP_AC_API_BACKEND_URL` |

**Tablas propias:**
- `attachment_counter_configs`
- `attachment_counter_row_state`
- `attachment_counter_processing_state`
- `attachment_counter_row_actions`
- `attachment_counter_manual_triggers`
- + múltiples tablas de analytics y summaries

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`, `organization_subscriptions`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY
POLLING_INTERVAL_SECONDS  # default: 70
API_RATE_LIMIT            # default: 300
```

**Deuda técnica:**
- Polling secuencial (no paralelo) — candidato a webhooks (Fase 3)

---

### dynamic-ddl

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/dynamic-ddl/` |
| **Puerto** | 3007 |
| **Product slug** | `dynamic-ddl` |
| **Stack** | FastAPI + background poller |
| **Modo** | Polling 90 segundos |
| **Frontend env** | `REACT_APP_DDL_BACKEND_URL` |
| **Rate limit Smartsheet** | 240 req/min (90% del límite) |

**Tablas propias:**
- `dynamic_ddl_configurations`
- `ddl_column_values`

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY
DDDL_PRODUCT_SLUG                    # default: "dynamic-ddl"
SMARTSHEET_MAX_CALLS_PER_MINUTE      # default: 240
DDL_MAX_RETRIES                      # default: 3
```

---

### textnow

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/textnow/` |
| **Puerto** | 3131 |
| **Product slug** | `textnow` |
| **Stack** | FastAPI |
| **Modo** | Webhook parcial + Telnyx para SMS |
| **Frontend env** | hardcoded (no env var) |
| **Cache** | in-memory 10min por org |

**Tablas propias:**
- `textnow_configurations`

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`

**Variables de entorno clave:**
```
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, APP_CRYPTO_KEY
SMARTTEXT_PRODUCT_SLUG       # default: "textnow"
CACHE_DURATION_SECONDS       # default: 600
# Telnyx credentials (ver .env.example)
```

---

### contact-book-connects

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/contact_book_connects/` |
| **Puerto** | 3058 |
| **Stack** | FastAPI + APScheduler |
| **Modo** | Nightly sync (11:59 PM UTC) |
| **Sin dependencias de otras herramientas** | ✓ |

**Tablas propias:**
- `smartsheet_user_sync_config`
- `smartsheet_synced_users`
- `external_contacts`
- `smartsheet_sync_logs`

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organizations`

---

### id-checker (servicio compartido)

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/id-checker/` |
| **Puerto** | 3002 |
| **Stack** | Flask (síncrono) |
| **Modo** | REST API, usado por frontend de todas las herramientas |
| **Frontend env** | `REACT_APP_ID_CHECKER_BACKEND_URL` |

**Usado por frontend de:** cell-history, bulk-attachment-uploader, dynamic-ddl, textnow

**Tablas compartidas que lee:**
- `user_profiles`, `smartsheet_tokens`, `organization_ss_api_base`

**Deuda técnica:**
- Flask síncrono — debería ser librería Python, no microservicio HTTP
- Cache in-memory no compartida entre instancias
- Migración planeada: convertir a `smarthub_core.smartsheet.picker`

---

### encrypt-tool (servicio compartido)

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/encrypt-tool/` |
| **Puerto** | 3051 |
| **Stack** | Flask (síncrono) |
| **Modo** | REST API para encriptar/desencriptar tokens |
| **Frontend env** | `REACT_APP_ENCRYPT_URL` |

**Tablas que lee:**
- `organizations`, `organization_ss_api_base`, `smartsheet_tokens`

**Deuda técnica:**
- Microservicio HTTP innecesario — cada llamada agrega 5-50ms de latencia de red
- Migración planeada: convertir a `smarthub_core.crypto`

---

### smart-qr (MVP — sandbox validado, sin desplegar)

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/smart-qr/` |
| **Puerto** | 3021 |
| **Product slug** | `smart-qr` |
| **Change-Agent** | `smart-qr-v1` |
| **Stack** | FastAPI + Pillow + qrcode + python-barcode + asyncpg |
| **Modo** | On-demand (**Generate codes**) + webhook Smartsheet directo + consumer de `stream:smart-qr` |
| **Frontend env** | `REACT_APP_SMARTQR_BACKEND_URL` |
| **Estado** | Fase 9a sandbox en verde; staging (9b) sin provisionar |

**Tablas propias:**
- `smartqr_configs`
- `smartqr_webhooks`
- `smartqr_row_states`
- `smartqr_usage_log`
- `smartqr_schema_migrations`

Migraciones en `backend/smart-qr/migrations/`, aplicadas al arrancar bajo advisory
lock. RLS activo sin políticas y sin grants a `anon`/`authenticated` (migración 008).

**Tablas compartidas que lee:**
- `user_organization_memberships`, `user_profiles` — **solo** para validar
  `X-Organization-ID`, y por la REST API de Supabase con el service role (sus
  policies exigen `auth.uid()`, así que asyncpg no ve ninguna fila).

**Variables de entorno clave:**
```
DATABASE_URL, DATABASE_SSL, REDIS_URL, APP_CRYPTO_KEY
SUPABASE_URL, SUPABASE_JWT_SECRET
SUPABASE_SERVICE_ROLE_KEY   # obligatoria fuera de local (membresía de org)
SMARTSHEET_MODE, SMARTSHEET_ACCESS_TOKEN   # token único por servicio (MVP)
WEBHOOK_PUBLIC_BASE_URL, CORS_ORIGIN, LOG_LEVEL
GATEWAY_INTERNAL_URL, GATEWAY_M2M_TOKEN
```

**Deuda técnica:** sin `/metrics`, sin DLQ ni dedup de stream, sin rate limit en
`qr-preview`, bolts en mock, un solo token Smartsheet. Ver
[SMARTQR_DEV_PLAN.md](../development/SMARTQR_DEV_PLAN.md).

---

## Librerías compartidas

### smarthub_core (en construcción — Phase 0)

| Campo | Valor |
|---|---|
| **Carpeta** | `shared/smarthub_core/` |
| **Tipo** | Paquete Python instalable (`pip install -e ./shared/smarthub_core[dev]`) |
| **Estado** | Phase 0 in progress — crypto, db.redis, db.supabase, smartsheet.client implementados |
| **Tests** | 97 (correr con `pytest` dentro del venv del paquete) |
| **Docs** | [docs/tools/smarthub_core/](smarthub_core/README.md) |

**Módulos:**
- `smarthub_core.crypto` — AES-GCM (reemplaza `crypto_helper.py` copiado en 6 backends)
- `smarthub_core.db.redis` — RedisClient + helpers cache-aside, dedup, locks, streams, rate limit
- `smarthub_core.db.supabase` — SupabaseService con httpx pool explícito + retry sync/async
- `smarthub_core.smartsheet.client` — SmartsheetClient scoped por (org, slug) + cache de token + rate limit

**Pendientes Fase 0:**
- `smarthub_core.worker.base` — clase base SmarthubWorker
- `smarthub_core.worker.health` — endpoint `/health` estandarizado

**Bugs cerrados al integrar:**
- #16 (Smartsheet errorCode 4000 not retried) — en `RETRYABLE_PATTERNS`
- #21 (HTTP/2 ConnectionTerminated not retried) — en `db.supabase` + `smartsheet.client`

---

### smart-qr

| Campo | Valor |
|---|---|
| **Carpeta** | `backend/smart-qr/` |
| **Puerto** | 3021 |
| **Product slug** | `smart-qr` |
| **Stack** | FastAPI + qrcode/Pillow + python-barcode · Postgres propio · Redis |
| **Modo** | On demand (**Generate codes**) + gateway webhook URL + consumer `stream:smart-qr` |
| **Frontend env** | `REACT_APP_SMARTQR_BACKEND_URL` |
| **Change-Agent** | `smart-qr-v1` |
| **Estado** | Staging ECS live · platform write SoT + tool mirror · **not** yet on gateway registrar (tool-owned lifecycle) |

**Tablas propias:**
- `smartqr_configs` — configuraciones (modo, columnas, estilo QR, logo)
- `smartqr_webhooks` — **mirror soft-fail**; write SoT = `platform_webhook_registrations`
- `smartqr_row_states` — hash de payload por fila (skip si no cambió)
- `smartqr_usage_log` — auditoría de escrituras/bolts (`user_id` desde migración 009)
- `smartqr_schema_migrations` — migraciones aplicadas

Todas con RLS activo sin políticas y sin grants a `anon`/`authenticated`
(migración `008_rls_lockdown.sql`). Cualquier tabla nueva debe agregarse ahí.

**Tablas compartidas que lee (vía service role / PostgREST):**
- `user_organization_memberships`, `user_profiles` — validar org + default org de browse
- `get_subscription_status` (RPC), `organization_subscriptions`, `organizations`, `pricing_list` — bolts
- Credenciales Smartsheet: **no** lee `smartsheet_tokens` directo; las resuelve encrypt-tool

**Variables de entorno clave:**
```
DATABASE_URL, DATABASE_SSL          # TLS auto para hosts no locales
REDIS_URL, RUN_STREAM_CONSUMER      # bolt-cap + stream:smart-qr
SUPABASE_URL, SUPABASE_JWT_SECRET   # obligatorio fuera de APP_ENV=local
SUPABASE_SERVICE_ROLE_KEY           # membership + billing reads (obligatorio fuera de local)
APP_ENV                             # solo "local" relaja la auth
CORS_ORIGIN                         # allowlist, nunca "*"
SMARTSHEET_MODE                     # live | mock
ENCRYPT_TOOL_INTERNAL_URL           # broker OAuth (obligatorio fuera de local)
SMARTSHEET_BROKER_SECRET            # m2m con encrypt-tool
SMARTQR_BILLING_MODE                # bolts (staging/prod) | mock (solo local)
WEBHOOK_PUBLIC_BASE_URL, APP_CRYPTO_KEY
GATEWAY_INTERNAL_URL, GATEWAY_M2M_TOKEN
```

**Dependencias de otras herramientas:**
- **encrypt-tool** — resuelve token OAuth por (org, user, `smart-qr`); scopes `READ_SHEETS`/`WRITE_SHEETS`/`ADMIN_WEBHOOKS`
- Pushea secretos de webhook al gateway por REST interno (`PUT /internal/v1/webhook-secrets/smart-qr/{org}`)
- El gateway publica en `stream:smart-qr` (`TOOL_STREAM_MAP`)
- Comparte contador Redis `bolt-cap:{org}:*` con I2C y el resto de tools

**Gaps conocidos (no bloquean primer deploy con 1 task):** sin `/metrics`,
sin DLQ durable, sin rate limiting, errores por fila no persistidos, webhook
inline. Ver [SMARTQR_DEV_PLAN.md](../development/SMARTQR_DEV_PLAN.md).

---

## Herramientas legacy (NO MODIFICAR)

| Carpeta | Razón |
|---|---|
| `backend/image-to-cell/` | Reemplazado por image-to-cell-3 |
| `backend/image-to-cell_2/` | Reemplazado por image-to-cell-3 |
| `backend/image-to-cell-donotuse/` | Deprecado explícitamente |

---

## Tabla de puertos

| Puerto | Herramienta |
|---|---|
| 3002 | id-checker |
| 3003 | bulk-attachment-uploader |
| 3005 | attachment-counter (API) |
| 3006 | attachment-counter (poller) |
| 3007 | dynamic-ddl |
| 3011 | image-to-cell-3 |
| 3014 | cell-history |
| 3021 | smart-qr |
| 3051 | encrypt-tool |
| 3058 | contact-book-connects |
| 3131 | textnow |

---

## Mapa de PRODUCT_SLUG por herramienta

El `product_slug` debe coincidir exactamente con el valor en la tabla `organization_subscriptions` de Supabase.

| Herramienta | PRODUCT_SLUG |
|---|---|
| image-to-cell-3 | `smart-image-to-cell` |
| smart-qr | `smart-qr` |
| cell-history | `single-cell-audit` |
| textnow | `textnow` |
| dynamic-ddl | `dynamic-ddl` |
| attachment-counter | (verificar en DB) |
| bulk-attachment-uploader | (verificar en DB) |
