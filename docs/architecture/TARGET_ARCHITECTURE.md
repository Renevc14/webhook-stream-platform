# Arquitectura Objetivo — Smarthub Platform

## Visión

Separar completamente la **recepción de eventos** (time-sensitive, debe responder en <100ms) del **procesamiento de datos** (CPU/IO-intensive, puede tardar segundos), usando Redis como sistema nervioso central y un Webhook Gateway compartido para todas las herramientas.

---

## Diagrama de capas

```
INTERNET
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  CAPA 1: EDGE                                            │
│  Route 53 + ACM + AWS WAF                                │
│  • DNS routing (Route 53)  • TLS/HTTPS (ACM)             │
│  • Rate limit por IP (WAF) • DDoS (Shield Standard)      │
│  • /webhook/* → regla WAF sin throttling                 │
└──────────────────────────┬──────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
        /api/*                    /webhook/*
              │                         │
    ┌─────────▼──────┐     ┌────────────▼──────────────────┐
    │  API Gateway   │     │     Webhook Gateway              │
    │  (por tool)    │     │     (COMPARTIDO, polyglot)       │
    │  configs, UI   │     │                                  │
    │  picker, auth  │     │  POST /webhook/{provider}/       │
    └────────────────┘     │       {tool_slug}/{org_id}       │
                           │                                  │
                           │  1. Valida firma      ~1ms      │
                           │  2. Challenge response ~1ms    │
                           │  3. Cache lookup Redis ~2ms    │
                           │  4. XADD stream:{tool} ~1ms    │
                           │  5. Responde 200 OK    ~5ms    │
                           │                                │
                           │  + Lifecycle Manager           │
                           │    (singleton, Redis lock)     │
                           └────────────┬───────────────────┘
                                        │
                           ┌────────────▼───────────────────┐
                           │         Redis                   │
                           │                                 │
                           │  STREAMS (colas persistentes):  │
                           │  stream:i2c       (+ stream:i2c:dlq)            │
                           │  stream:cell-history       (+ :dlq)             │
                           │  stream:attachment-counter (+ :dlq)             │
                           │  stream:dynamic-ddl        (+ :dlq)             │
                           │  stream:textnow            (+ :dlq)             │
                           │  stream:platform-events  ←bus                   │
                           │                                                  │
                           │  CACHE (cache-aside, TTL, ciphertext only):     │
                           │  webhook-secrets:{org}:{slug}  (JSON map)       │
                           │  org-config:{org}:{slug}                        │
                           │  ss-token:{org}:{slug}                          │
                           │  workspaces:{org}:{slug}                        │
                           │                                                  │
                           │  DEDUP (SET NX + TTL):                           │
                           │  dedup:{tool}:{config}:{row}:{attachment}       │
                           │                                                  │
                           │  RATE LIMIT (Sorted Set):                        │
                           │  ratelimit:ss-api:{org}:{slug}                  │
                           │  (per API key — tokens are scoped org+slug)     │
                           │                                                  │
                           │  LOCKS (SET NX EX, token-checked release):       │
                           │  lock:lifecycle:manager  (TTL 600s, hb 60s)     │
                           │  lock:fetch:{cache-key}  (TTL 5s, stampede)     │
                           └────────────┬───────────────────┘
                                        │
          ┌──────────┬──────────┬───────┴──┬──────────┐
          ▼          ▼          ▼          ▼          ▼
    ┌──────────┐┌──────────┐┌──────┐┌──────────┐┌──────────┐
    │ worker   ││ worker   ││work  ││ worker   ││ worker   │
    │ i2c      ││ cell-h   ││att-c ││ ddl      ││ textnow  │
    │ 1.5GB    ││ 512MB    ││256MB ││ 512MB    ││ 256MB    │
    │ 1 vCPU   ││ 0.5vCPU  ││0.25c ││ 0.5vCPU  ││ 0.25vCPU │
    │ autoscale││ autoscale││auto  ││ autoscale││ autoscale│
    └──────────┘└──────────┘└──────┘└──────────┘└──────────┘
          │          │          │          │          │
          └──────────┴──────────┴──────────┴──────────┘
                                │
                   ┌────────────▼───────────┐
                   │       Supabase          │
                   │  PostgreSQL · RLS       │
                   │  fuente de verdad       │
                   └────────────────────────┘
```

---

## Componentes clave

### Webhook Gateway (compartido, polyglot)

Un solo servicio FastAPI deployado con 2+ replicas. Es el único componente con URL pública para webhooks de **cualquier provider externo** (Smartsheet hoy; HubSpot, Slack, Stripe, Twilio cuando se agreguen).

**URL pattern:** `POST /webhook/{provider}/{tool_slug}/{org_id}`

El path segment `{provider}` selecciona qué handler procesa la request — cada provider tiene su propio algoritmo de firma, su propio challenge (o ninguno) y su propio shape de payload, pero todos producen el mismo envelope versionado para los streams.

**Responsabilidades:**
- Dispatch al handler del provider via routing registry
- Validar firma del provider (HMAC-SHA256, HMAC-SHA1, signed timestamp, según el provider — ver tabla en MIGRATION_PLAN.md "Multi-provider design")
- Manejar challenge si el provider lo requiere (Smartsheet: sync challenge; Slack: URL verification con `challenge` field; HubSpot/Stripe/Twilio: ninguno)
- Lookup de configuración activa en Redis (cache)
- Encolar evento normalizado en `stream:{tool_slug}` (no incluye `provider` en stream key — un tool que recibe de múltiples providers tiene un solo stream; el envelope incluye `provider` field)
- Lifecycle Manager unificado: registrar/renovar/limpiar webhooks de TODOS los tools y TODOS los providers
- Responder 2xx — nunca bloquear en procesamiento

**Lo que NO hace:**
- Procesar payloads (download imágenes, hacer API calls al provider, tocar Supabase) — eso es responsabilidad del worker que consume el stream
- Code provider-specific business logic — solo signature validation y normalization a envelope

### Redis Streams vs. listas simples

```
Lista simple (problemática):
  RPUSH → Worker hace LPOP → procesa → job desaparece
  Si el worker cae: job perdido para siempre ❌

Redis Stream (correcto):
  XADD → Worker hace XREADGROUP (job queda "pending")
       → Worker procesa
       → Worker hace XACK (recién aquí se elimina)
  Si el worker cae: Redis reasigna a otro worker ✅
  Si todos los workers caen: job queda en "pending" ✅
```

### Caching strategy

**Regla única:** Postgres (Supabase) es la fuente de verdad para todo lo que tiene fila en una tabla. Redis es la fuente de verdad solo para datos que no existen en DB (dedup, rate limit, locks distribuidos).

Todo lo respaldado por DB usa **cache-aside**: lazy load on read miss, `DEL` explícito on write. Sin write-through, sin write-behind. Cache-aside es el único patrón que sobrevive a Redis caído sin pérdida de datos.

#### Especificación por tipo de clave

| # | Key | Patrón | Source of truth | TTL | Invalidación | Fallback (Redis down) |
|---|---|---|---|---|---|---|
| 1 | `webhook-secrets:{org}:{slug}` | cache-aside | Postgres (`imagetocell_webhooks`, `worksheet_webhooks`) | 300s | `DEL` al crear/rotar/eliminar webhook | Read directo a Postgres |
| 2 | `org-config:{org}:{slug}` | cache-aside | Postgres (`imagetocell_config`, etc.) | 60s + jitter ±10% | `DEL` en cada UPDATE/INSERT/DELETE de config | Read directo a Postgres |
| 3 | `ss-token:{org}:{slug}` | cache-aside (**ciphertext only**) | Postgres (`smartsheet_tokens`, UNIQUE(org, slug)) | 600s | `DEL` en rotación/revocación de token | Read directo + decrypt local |
| 4 | `workspaces:{org}:{slug}` | cache-aside | Smartsheet API (read-mostly remote) | 300s | TTL + botón "force refresh" en UI | Llamada directa a Smartsheet |
| 5 | `dedup:{tool}:{config}:{row}:{attachment}` | **Redis SoT** | Redis | 600s | TTL natural (no manual) | In-memory por proceso |
| 6 | `ratelimit:ss-api:{org}:{slug}` | **Redis SoT** (sorted set, sliding window) | Redis | window 60s, key TTL 120s | Lua atómico `ZREMRANGEBYSCORE+ZADD` | Token bucket en proceso |
| 7 | `i2c:processing:{att_id}` · `i2c:row:{cfg}:{row}` · `i2c:completed:{att_id}` | Redis SoT | Redis | 600s | `SET NX EX 600` | In-memory por proceso |

**Shape de cache values:**
- `webhook-secrets:{org}:{slug}` → JSON map `{webhook_id: encrypted_secret}` (una org+slug tiene un webhook por sheet)
- `ss-token:{org}:{slug}` → JSON `{"ciphertext": str, "base_url": str}`. La key contiene **siempre ciphertext** (nunca plaintext) + el base URL regional (US/EU/Gov). Se invalida juntos en token rotation.
- `org-config:{org}:{slug}` → JSON list de configs activas para esa (org, slug)
- `workspaces:{org}:{slug}` → JSON list del response de Smartsheet

#### Cache stampede prevention (claves 1–4)

Para evitar dogpile cuando expiran múltiples instancias simultáneas (típico tras un deploy):

```
val = GET key
if val: return val
if SET NX EX 5 lock:fetch:{key}:
    fetch from DB → SETEX key value TTL → DEL lock:fetch:{key}
else:
    sleep 50ms + jitter → retry GET key (1 vez)
    if still miss: fetch from DB sin lock (no bloquear infinito)
```

#### Regla de Redis-down

Todo acceso a Redis pasa por un helper que retorna `None` (sentinel) en `RedisError`. Caller usa el sentinel para fallback. **Una caída de Redis degrada latencia, nunca corrección** — excepto en claves 5/6/7 donde se acepta dedup local en lugar de cross-instance (con log warning explícito).

#### Razonamiento de TTLs

- **Webhook secrets / SS tokens (5–10 min):** invalidación es explícita en rotación; el TTL solo importa si la invalidación falla.
- **Org config (60s):** cambios interactivos en admin UI deben verse "casi inmediato"; 60s es el upper bound de staleness en peor caso.
- **Workspaces (5 min):** lista externa read-mostly; si el usuario acaba de crear un workspace, hay botón refresh.
- **I2C operacionales (600s):** paridad con código actual; cambiar storage sin cambiar semántica para no mezclar regresiones.

---

### Database update sequence

**Patrón siempre:** `(1) write DB → (2) DEL cache → (3) return`. Si el `DEL` falla, se loguea y el TTL maneja el resto. **Nunca** escribir el cache antes que la DB.

#### Escenario 1 — Editar config en admin UI

```
Frontend  POST /api/configs/{id}
  api-gateway:
    1. UPDATE imagetocell_config SET ... WHERE id = ?
    2. await redis.delete(f"org-config:{org}:{slug}")    # idempotente, log on error
    3. return 200
```

#### Escenario 2 — Rotar Smartsheet token

```
Frontend  POST /api/tokens/rotate
  api-gateway:
    1. encrypted = CryptoService.encrypt(new_pat)
    2. UPDATE smartsheet_tokens SET token_encrypted=? WHERE org=? AND slug=?
    3. await redis.delete(f"ss-token:{org}:{slug}")
    4. await redis.delete(f"workspaces:{org}:{slug}")    # token nuevo puede ver otros workspaces
    5. return 200
```

> Probar el token contra Smartsheet **después** del commit (endpoint separado o background task), nunca dentro de la transacción — un outage transitorio de Smartsheet no debe bloquear la rotación.

#### Escenario 3 — Lifecycle Manager registra webhook nuevo

```
Lifecycle Manager (singleton, holds lock:lifecycle:manager TTL 600s, hb 60s):
  1. Smartsheet API: createWebhook(scope, callback) → returns shared_secret
  2. encrypted_secret = CryptoService.encrypt(shared_secret)
  3. INSERT imagetocell_webhooks (... shared_secret_encrypted, status='NEW_NOT_VERIFIED')
  4. INSERT worksheet_webhooks (...)                                     # mirror dual-write actual
  5. await redis.delete(f"webhook-secrets:{org}:{slug}")                 # próximo evento re-fetcha
  6. return
```

#### Escenario 4 — I2C worker procesa attachment

```
Worker consumes XREADGROUP stream:i2c:
  1. set_ok = await redis.set(
         f"dedup:i2c:{config}:{row}:{attachment}", "1", nx=True, ex=600
     )
     if not set_ok: XACK + skip                          # otro worker la tiene
  2. (process: download → resize → upload to cell)
  3. UPDATE imagetocell_row_state ...                    # Postgres = SoT del estado completado
  4. await redis.set(f"i2c:completed:{att_id}", "1", ex=600)
  5. XACK stream:i2c
  6. XADD stream:platform-events {type:image_processed, ...}
```

> **Orden importante:** dedup ANTES del trabajo, DB write ANTES de XACK, platform-events DESPUÉS de XACK. Si el worker muere entre 5 y 6, el platform-event se pierde — aceptable para Phase 0–2 (Bulk Uploader retry idempotente). Si en Fase 4 se necesita exactly-once cross-bus, se implementa transactional outbox en Postgres barrido por un publisher.

#### Cross-process invalidation: regla de api-gateway

**Toda escritura admin a Postgres pasa por `api-gateway`** (no por PostgREST directo desde el frontend). Esto garantiza que hay un proceso Python en el path que puede ejecutar el `DEL` después del commit. Alternativas como Postgres trigger + `pg_notify` + daemon listener añaden infra sin beneficio a la escala actual.

---

### Platform Events Bus (cross-tool)

El stream `stream:platform-events` permite que una herramienta publique eventos que otras consumen sin acoplamiento directo.

```
I2C Worker termina:
  XADD stream:platform-events {
    type: "image_processed",
    tool: "i2c",
    org_id: "...",
    worksheet_id: ...,
    row_id: ...,
    config_id: "..."
  }

Bulk Uploader Worker (consumer del bus):
  XREADGROUP stream:platform-events "bulk-uploader"
  → filtra type="image_processed"
  → ejecuta lógica de bulk upload
```

### smarthub_core (librería Python compartida, multi-provider)

Reemplaza los archivos copy-paste (`crypto_helper.py`, `sheetpicker.py`, `encrypt_service.py`) con un paquete Python interno importable por todos los workers. Los módulos provider-agnostic viven en la raíz; los provider-specific viven bajo `platforms/{provider}/`.

```
shared/smarthub_core/
├── crypto/                          # provider-agnostic
│   └── token_cipher.py              # AES-GCM, sin HTTP hop
├── db/                              # provider-agnostic
│   ├── supabase.py                  # cliente con retry y connection pooling
│   └── redis.py                     # streams, cache, dedup, locks, rate limit
├── platforms/                       # módulos por provider externo
│   ├── shared/
│   │   ├── token_resolver.py        # interface abstracta TokenResolver
│   │   ├── webhook_base.py          # BaseWebhookHandler abstract
│   │   └── oauth.py                 # refresh helpers para providers OAuth
│   ├── smartsheet/
│   │   ├── client.py                # SmartsheetClient(org_id, product_slug)
│   │   ├── picker.py                # workspace/sheet picker con cache Redis
│   │   └── webhook.py               # HMAC SHA-256 + sync challenge
│   ├── hubspot/                     # cuando se agregue
│   │   ├── client.py
│   │   ├── webhook.py               # HMAC SHA-256 con timestamp
│   │   └── oauth.py                 # refresh token cada ~6h
│   ├── slack/                       # cuando se agregue
│   ├── stripe/                      # cuando se agregue
│   └── ...
└── worker/                          # provider-agnostic
    ├── base.py                      # SmarthubWorker base class
    └── health.py                    # compute_health_report
```

**Principio:** todo el core (`crypto`, `db`, `worker`) es provider-agnostic. Los workers consumen streams sin saber qué provider originó el evento — el envelope incluye `provider` y `event_type` para que el worker decida cómo parsear `raw`.

### Worker base class (patrón estándar)

Todos los workers heredan de `SmarthubWorker`:

```python
class SmarthubWorker:
    tool_slug: str          # identifica el stream a consumir
                            # group  = tool_slug
                            # consumer = f"{tool_slug}-{pod_id_uuid}"

    async def run(self):
        async for job in self.stream.consume(group=self.tool_slug, consumer=self.consumer):
            async with self.dedup_lock(job):    # Redis SET NX EX 600
                try:
                    result = await self.process(job)
                    # 1. DB write (source of truth) inside process()
                    # 2. ack BEFORE publish — at-least-once on platform-events is acceptable
                    await self.stream.ack(job)
                    await self.publish_result(result)  # platform-events (best-effort)
                except RetryableError as e:
                    await self.stream.requeue(job, delay=backoff(job.attempts))
                except FatalError as e:
                    await self.stream.send_to_dlq(
                        job,
                        dlq_stream=f"stream:{self.tool_slug}:dlq",
                        reason=str(e),
                    )

    async def process(self, job: Job) -> Result:
        raise NotImplementedError
```

#### Stream payload envelope (versionado)

Todo evento publicado en un `stream:{tool}` o en `stream:platform-events` usa este envelope:

```json
{
  "v": 1,
  "event_id": "uuid-v4",
  "tool": "i2c",
  "org_id": "uuid",
  "config_id": "uuid",
  "received_at": "2026-04-30T15:00:00Z",
  "raw": { ... }
}
```

- `v` permite migrar el shape sin romper consumers viejos.
- `event_id` es la clave para dedup downstream.
- `received_at` es el timestamp en que el Gateway recibió el webhook (no el del worker).
- `raw` es el payload original de Smartsheet, sin transformar.

#### DLQ por stream

Cada stream tiene su DLQ paralela: `stream:{tool}:dlq`. El worker hace `XADD` a la DLQ con `{event_id, reason, traceback, original_payload}` cuando una `FatalError` cierra el procesamiento. La DLQ es read-only para los workers; un proceso operacional separado (futuro) puede inspeccionar y re-encolar.

#### Consumer group naming

- `group = tool_slug` (ej: `i2c`, `cell-history`)
- `consumer = f"{tool_slug}-{pod_id_uuid}"` (un nombre único por pod para que `XPENDING` sea atribuible y dos pods no clobberen sus pending lists)

#### Lifecycle Manager Redis lock

- Key: `lock:lifecycle:manager`
- Acquire: `SET lock:lifecycle:manager <pod_uuid> NX EX 600`
- Heartbeat: cada 60s, extender el TTL con un Lua script que valida el token (`if GET == pod_uuid then EXPIRE 600 else 0`)
- Release on shutdown: Lua script token-checked (`if GET == pod_uuid then DEL else 0`)
- Si el holder muere sin liberar, otro pod toma el lock cuando expira a los 600s

> **Token scoping note:** `smartsheet_tokens` has a `UNIQUE(organization_id, product_slug)` constraint.
> Each tool has its own Smartsheet PAT per org. `SmartsheetClient` must always receive both
> `org_id` and `product_slug` — never resolve credentials with `org_id` alone.
> Each API key has its own rate limit bucket — tools in the same org do not compete for rate limits.

> **Security rule:** Tokens en Redis SIEMPRE están en ciphertext. La clave AES (`APP_CRYPTO_KEY`) solo vive en el proceso que decripta justo antes de usar el token. Nunca cachear plaintext en Redis ni en logs.

---

## Estructura del monorepo objetivo

```
smarthub/
├── CLAUDE.md
├── docs/
│   ├── architecture/
│   ├── development/
│   └── tools/
│
├── shared/
│   └── smarthub_core/          # librería Python compartida
│       ├── pyproject.toml
│       └── src/smarthub_core/
│
├── services/
│   ├── webhook-gateway/        # UN solo servicio, siempre encendido
│   └── api-gateway/            # API REST unificada (futuro)
│
├── workers/                    # un folder por herramienta
│   ├── image-to-cell/
│   ├── cell-history/
│   ├── attachment-counter/
│   ├── dynamic-ddl/
│   ├── textnow/
│   └── bulk-uploader/
│
├── frontend/                   # sin cambios estructurales
│
└── infra/
    ├── docker-compose.yml      # desarrollo local completo
    └── railway/                # configuración de producción
```

---

## Despliegue en AWS

**Infraestructura existente (ya en producción):**

| Componente | Servicio AWS | Rol |
|---|---|---|
| DNS | Route 53 | Routing hacia ALB y Amplify |
| TLS | ACM | Certificados para ALB y CloudFront |
| Frontend | AWS Amplify | CDN + deploy React SPA |
| Container registry | ECR | Imágenes de todos los tools |
| Orchestration | ECS Fargate | Un service por herramienta |
| Load balancer | Shared ALB | Path-based routing a todos los ECS services |
| Secrets | AWS Secrets Manager | Un secret por herramienta |
| Observabilidad | CloudWatch | Logs y métricas |
| CI/CD | GitHub Actions | Build y push a ECR, deploy en ECS |

**Servicios nuevos a provisionar en la migración:**

```
Proyecto AWS: Smarthub
│
├── ElastiCache for Redis      cache.t3.micro    always-on
│   (cluster mode disabled — escalar a cache.t3.small con carga real)
│
├── ECS Service: webhook-gateway    2 tasks min    512MB    always-on
│   ALB rule: /webhook/* → Target Group webhook-gateway
│
├── ECS Service: worker-i2c         1-4 tasks      1.5GB    auto-scaling
├── ECS Service: worker-cell-hist   1-2 tasks      512MB    auto-scaling
├── ECS Service: worker-att-count   1-2 tasks      256MB    auto-scaling
├── ECS Service: worker-ddl         1-2 tasks      512MB    auto-scaling
├── ECS Service: worker-textnow     1-2 tasks      256MB    auto-scaling
└── ECS Service: worker-bulk-up     1-3 tasks      1GB      auto-scaling
```

**Un solo Dockerfile, múltiples entrypoints via `APP_MODE`:**
```dockerfile
CMD ["sh", "-c", "if [ \"$APP_MODE\" = 'worker-i2c' ]; then \
      python -m workers.image_to_cell; \
    elif [ \"$APP_MODE\" = 'webhook-gateway' ]; then \
      uvicorn services.webhook_gateway.main:app --host 0.0.0.0 --port 8000 --workers 4; \
    fi"]
```

**ALB path-based routing (adición a reglas existentes):**
```
/webhook/* → Target Group: webhook-gateway  (prioridad alta, siempre activo)
/api/*     → Target Group: api-gateway      (existente)
```

---

## Capacidad proyectada

| Configuración | Usuarios concurrentes | Orgs | Webhooks/min | Infra adicional |
|---|---|---|---|---|
| Actual (ECS, sin Redis) | ~40 | 10 | ~200 | Solo ECS actuales |
| Fase 1 (I2C + ElastiCache) | ~80 | 15 | ~400 | + ElastiCache t3.micro |
| Fase 2 (Gateway + workers) | ~300 | 50 | ~2,000 | + ECS services worker |
| Fase 3 (Gateway completo) | ~1,000+ | 200+ | ~10,000 | + scale-out ECS + ElastiCache t3.small |
