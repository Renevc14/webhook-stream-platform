# Pulse — Design Rationale, Patterns, Anti-patterns and Migration Approach

> **Audience.** Engineers (current and future) who need to understand **why** Pulse is shaped the way it is, **what** patterns and anti-patterns drove the design, **what trade-offs** we accepted, and **how** we are migrating from the current architecture to it.
>
> **Companion docs.**
> - [`CURRENT_STATE.md`](CURRENT_STATE.md) — snapshot per-tool of where the legacy architecture is today
> - [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) — formal spec of Pulse (components, contracts, key conventions)
> - [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) — phase-by-phase execution plan
> - [`DEPENDENCY_MAP.md`](DEPENDENCY_MAP.md) — what breaks what
>
> This document is the **decision log**: it explains, in one place, the load-bearing reasoning so future revisions don't lose institutional context.

---

## 1. Context — qué es Pulse y por qué existe

**Pulse** es la arquitectura objetivo del backend de Smarthub. Reemplaza el modelo actual (8 backends FastAPI independientes, cada uno con su propio cliente Smartsheet, su propio handling de webhooks, su propio dedup en memoria) por una capa unificada compuesta de:

- Una **librería compartida** (`smarthub_core`) que centraliza crypto, Redis, Supabase, clientes a proveedores externos, y un worker base
- Un **Webhook Gateway** polyglot que recibe eventos de cualquier proveedor (Smartsheet, HubSpot, Slack, Stripe, ...) y los normaliza
- **Redis Streams** como bus de decoupling entre el gateway y los workers de cada tool
- **ElastiCache Valkey** como runtime de cache + dedup + locks + rate limit + streams
- **Idempotencia a nivel DB** para todas las operaciones billing-críticas

El nombre "Pulse" alude al **latido del sistema**: el flujo continuo de eventos que entran al gateway, se publican en streams, se consumen por workers, se procesan, y se persisten en Postgres con observabilidad consistente.

### Motivación cliente

Smarthub crece a ~5-8 tools nuevas por año conectándose a múltiples proveedores externos. La arquitectura actual:

- Duplica código en cada backend (6 copias del helper de crypto, cada cliente Smartsheet diferente)
- No escala más allá de 1 instancia ECS por tool sin doble-procesamiento de webhooks
- Acumula deuda operacional (cada tool tiene su propia rate-limit, dedup, observabilidad)
- Hace que agregar un nuevo proveedor requiera reescribir gran parte del stack

Pulse resuelve esto **a costo de complejidad operacional adicional** (Redis, streams, gateway) que el negocio justifica con el crecimiento proyectado.

---

## 2. Patrones de diseño aplicados — justificación

Esta sección documenta **cada patrón load-bearing** de Pulse, por qué se eligió y qué problema concreto resuelve.

### 2.1 Strangler Fig (para la migración)

**Aplicado a:** la migración de cada backend desde el legacy hacia `smarthub_core`, y eventualmente del gateway por-tool hacia el Webhook Gateway compartido.

**Justificación.** Los caminos críticos de billing no toleran horas de downtime. Hacer un big-bang con 8 backends en simultáneo:

1. Bloquearía meses de capacidad del equipo
2. Tendría un blast-radius enorme si algo sale mal
3. No tiene rollback granular — todo o nada

El Strangler Fig nos permite **migrar módulo por módulo dentro de un backend** (crypto → supabase → smartsheet) y **backend por backend** (i2c-3 primero, después los demás), con cada paso independientemente revertible. Cada PR cierra un sub-objetivo medible.

**Implementación.** Por capas (ver Sección 7 para detalles):
1. La librería compartida (`smarthub_core`) se construye y publica primero — independiente, sin consumidores
2. Cada backend adopta `smarthub_core` módulo por módulo, vía shims que preservan la API pública del legacy mientras intercambian la implementación interna
3. El Webhook Gateway nuevo coexiste con los gateways per-tool actuales hasta que el último tool sea migrado, momento en el cual los receivers per-tool se eliminan
4. El código legacy se elimina solo cuando `grep` confirma que ya nadie lo importa

### 2.2 Cache-Aside (Postgres = source of truth, Redis = optimización)

**Aplicado a:** tokens de Smartsheet, configuración por org, lista de workspaces, webhook secrets.

**Justificación.** Redis es opcional en nuestro sistema, no crítico:

- Postgres tiene la fila autoritativa de cada token, config, workspace
- Redis se usa para evitar hits repetidos a Postgres en datos read-mostly
- Si Redis cae, queremos que la app **siga funcionando con peor latencia**, no que se rompa

Cache-aside es el único patrón que cumple esa propiedad:

- Read: si hay miss o RedisError → fallback a Postgres
- Write: write a Postgres, después `DEL` la cache key. Próximo read re-popula.

Alternativas que rechazamos:

- **Write-through** (escribir a Redis y Postgres simultáneamente): si Redis está down, no podemos hacer writes. Inaceptable.
- **Write-behind** (escribir a Redis, sync async a Postgres): si Redis cae antes del sync, perdemos datos. Inaceptable para billing.

**Stampede prevention.** Cuando una key cachea hace miss, múltiples requests podrían disparar el mismo refetch a Postgres simultáneamente (thundering herd). Para evitarlo, el helper de cache-aside en `smarthub_core` adquiere un `lock:fetch:{key}` por 5 segundos con backoff y reintenta el GET una vez antes de hacer el fetch sin lock. Documentado en [`TARGET_ARCHITECTURE.md#caching-strategy`](TARGET_ARCHITECTURE.md).

### 2.3 Polyglot Adapter (módulos por proveedor)

**Aplicado a:** la estructura `smarthub_core.platforms.{provider}/`.

**Justificación.** Smarthub se conectará progresivamente a Smartsheet, HubSpot, Slack, Stripe, Twilio, y otros. Cada proveedor tiene:

- Su propio algoritmo de firma de webhook (HMAC-SHA256 vs SHA1 vs signed timestamp)
- Su propio modelo de auth (PAT vs OAuth con refresh token vs API key)
- Sus propios endpoints, rate limits y patterns de retry
- Sus propios shapes de payload

Pero todos enchufan a la misma infraestructura compartida (cache, locks, streams, worker base). Si dejamos que cada implementación viva donde quiera, terminaríamos con código Smartsheet-specific embedded en lugares que ningún otro proveedor podría reutilizar.

La estructura `platforms/{provider}/` con interfaces abstractas en `platforms/shared/` (TokenResolver, BaseWebhookHandler, OAuthRefresher) garantiza:

1. Agregar un nuevo proveedor es **aditivo**: crear `platforms/{nuevo}/`, implementar las 3 interfaces, listo
2. Cada provider puede tener idiosincrasias propias sin contaminar el core
3. Tests por proveedor están aislados
4. Plug-and-play real, no acoplamiento implícito

Esto es esencialmente el patrón **Hexagonal Architecture / Ports and Adapters** aplicado a un dominio chico (integraciones con APIs externas).

### 2.4 Distributed Singleton via Token-Checked Lock

**Aplicado a:** Webhook Lifecycle Manager (registración, renovación, cleanup de webhooks de Smartsheet).

**Justificación.** El Lifecycle Manager hace trabajo de housekeeping que **debe ejecutarse exactamente una vez por cluster por ciclo**, no una vez por instancia ECS. Si dos pods corren el ciclo simultáneamente:

- Duplican llamadas a Smartsheet (rate limit penalty)
- Pueden borrar webhooks que el otro pod está creando (race condition)
- Métricas se duplican

Redis con `SET NX EX 600` (key `lock:lifecycle:manager`) garantiza que solo un pod sea el holder del lock en un momento dado. El holder ejecuta el ciclo y libera (o el TTL expira si crashea). **Heartbeat cada 60s** extiende el TTL mientras el holder esté vivo, sin esperar al expire natural.

El release es **token-checked**: el holder guardó un UUID al adquirir, y solo libera si el value en Redis sigue siendo ese UUID. Esto previene la falla clásica del "lock expirado donde el holder cree que sigue dueño y libera el lock de OTRO holder".

Implementación detallada en `smarthub_core.db.redis.RedisClient.acquire_lock / release_lock` usando Lua scripts atómicos.

### 2.5 Bounded Concurrency Everywhere

**Aplicado a:** httpx pool (Supabase + Smartsheet), asyncio.Semaphore (per-org concurrency), ThreadPoolExecutor (legacy, eventualmente reemplazado).

**Justificación.** Supabase compute (Pro micro) tiene un cap de ~50 conexiones simultáneas presupuestadas para la app. La librería `supabase-py` default es `httpx.Limits(max_connections=100)`. Multiplicado por 4 uvicorn workers × 8 backends = 3200 conexiones potenciales contra un servidor que aguanta 50.

Sin límites explícitos, bajo carga el cluster colapsa con `Server disconnected` y similar. Con límites:

- `SUPABASE_MAX_CONNECTIONS=5` per worker × 4 workers × 8 backends = 160 max. Manejable.
- `asyncio.Semaphore` en backfill bound el paralelismo a `BACKFILL_MAX_WORKERS` (default 4)
- Lua sliding window rate limit en Redis bound los hits a Smartsheet a 275/min cluster-wide

Sin caps explícitos es un anti-pattern (ver Sección 4). Pulse los hace **first-class**.

### 2.6 Event-driven Decoupling via Redis Streams

**Aplicado a:** la comunicación gateway ↔ workers (futuro Phase 3), y workers ↔ workers cross-tool (futuro Phase 5).

**Justificación.** Hoy el receiver per-tool es responsable de **todo**: recibir webhook, validar firma, ack a Smartsheet en <5s, procesar la imagen, escribir a Postgres. Si el procesamiento tarda 30s, Smartsheet timeout y reintenta.

Con Streams:

```
Gateway (delgado, <100ms):  recibe webhook → valida firma → XADD stream:i2c → ack 200
Worker (gordo, sin SLA):    XREADGROUP stream:i2c → procesa imagen → escribe Postgres → XACK
```

Beneficios:
- **Backpressure**: si los workers están saturados, los mensajes se acumulan en el stream sin perder. Hoy se pierden o reintentan caóticamente.
- **Multi-worker**: N workers pueden consumir del mismo stream con consumer groups. Hoy es 1 ECS task por tool con scaling vertical solamente.
- **Cross-tool reactivity**: `stream:platform-events` es el bus donde un tool publica "image_processed" y otros tools (Bulk Uploader, Reports) reaccionan. Hoy es acoplamiento implícito.
- **DLQ por tipo**: malos mensajes van a `stream:{tool}:dlq` para revisión humana. Hoy son logs perdidos.
- **Replay**: con XCLAIM podemos reprocesar mensajes que un worker no logró ACK'ar. Hoy se pierden.

### 2.7 Idempotency at Multiple Layers

**Aplicado a:** billing — `imagetocell_usage_log`, dedup de attachments, bolt cap deduction.

**Justificación.** El billing es la operación más sensible del sistema. Un bug que double-bills es customer-visible, regulatorio, y rompe confianza. **Defense in depth**:

1. **Capa DB**: `imagetocell_usage_log` tiene un partial UNIQUE INDEX en `(organization_id, worksheet_id, row_id, image_column_id, image_filename) WHERE success = true` (SH-98). Postgres rechaza duplicates con SQLSTATE 23505 — **el bug del double-bill es imposible a nivel arquitectónico**.
2. **Capa application**: el wrapper `log_image_conversion` en Python detecta 23505 y lo trata como no-op (silently returns None, INFO log).
3. **Capa worker**: `dedup:i2c:processing:{att_id}` en Redis con `SET NX EX 600` evita que dos workers procesen el mismo attachment, **independiente de si el DB ya tiene la fila**.
4. **Capa Smartsheet**: `Smartsheet-Change-Agent` header en todas nuestras escrituras a Smartsheet evita que nuestros propios cambios disparen webhooks recursivos.

Cada capa por sí sola sería suficiente en la mayoría de los casos. Las 4 juntas hacen que **un bug en una capa no produzca daño cliente-visible** porque las otras 3 lo atrapan.

### 2.8 Tolerant Inputs, Strict Logic

**Aplicado a:** la deserialización de payloads de proveedores externos (webhooks, callbacks).

**Justificación.** Aprendizaje doloroso de SH-104 production incident: tener un Pydantic enum estricto en `WebhookEvent.objectType` causó que payloads con eventos `discussion`/`proof` rechazaran **todo el payload**, perdiendo los attachments válidos del mismo batch.

El principio:

- **Capa de parsing** (boundary con el mundo externo): aceptar cualquier valor razonable. No imponer nuestras restricciones a un sistema que no controlamos.
- **Capa de lógica** (nuestro código): procesar solo lo que conocemos, ignorar el resto, **loguear lo desconocido** para visibilidad sin crashear.

Aplicación concreta:
- `event_type` y `object_type` en `WebhookEvent` son `str`, no `Enum`. Las clases Enum siguen como **catálogo de referencia** para que el código que procesa pueda comparar, pero la validación de input es laxa.
- Cuando aparece un `objectType` desconocido, el route loguea un mensaje INFO "Unknown objectTypes seen and ignored" con visibilidad temprana, sin tirar la conexión.

Esto es lo opuesto al instinto "fail fast" que se aplica internamente — en boundaries externos, fail-fast contra inputs desconocidos es frágil.

### 2.9 Bulkhead (aislamiento por tool)

**Aplicado a:** un stream por tool, un consumer group por tool, una DLQ por tool, eventualmente un service ECS por tool.

**Justificación.** Si un tool entra en mal estado (deploy malo, regresión, sheet con datos corruptos), **no debería arrastrar a los demás**. Hoy, los 8 backends son procesos independientes, lo cual ya da algún aislamiento. Pulse mantiene y mejora ese aislamiento:

- `stream:i2c`, `stream:cell-history`, `stream:bau`, etc. son streams separados — un backlog en uno no afecta a otro
- DLQ per tool — los errores fatales de un tool se acumulan en `stream:{tool}:dlq` sin contaminar la cola de mensajes válidos
- Worker pools separados — `worker-i2c` y `worker-cell-history` son ECS services distintos con escala independiente
- Failure containment — si Redis se llena de mensajes de un tool, los demás tools no ven impacto (cada uno tiene su propio backlog)

### 2.10 Outbox Pattern — DEFERIDO

**No aplicado todavía.** Lo registramos porque es relevante para Phase 5.

**Problema que resolvería:** entre `XACK stream:i2c` (worker acknowledge) y `XADD stream:platform-events` (notificar a otros tools), si el worker muere, perdemos el evento `image_processed`. Bulk Uploader no se entera, no procesa.

**Solución estándar (outbox):**
1. Worker escribe a Postgres en una tabla `outbox` con el evento a publicar
2. Worker hace XACK
3. Un proceso separado lee el outbox y publica a Redis, marca como published

**Por qué no lo aplicamos hoy:** la pérdida ocasional de un `image_processed` es aceptable porque Bulk Uploader tiene su propio retry/reconcile contra `imagetocell_usage_log`. Si la frecuencia de pérdida crece o un tool nuevo no tiene retry propio, ahí implementamos outbox.

---

## 3. Componentes — qué necesitamos

### 3.1 Inventario completo

| Componente | Status | Responsabilidad | Dependencias |
|---|---|---|---|
| `shared/smarthub_core/` library | ✅ Built (SH-9 merged via PR #131) | Crypto, Redis client, Supabase client, Smartsheet client, worker base, multi-provider scaffolding | `redis>=5.0`, `httpx`, `supabase`, `smartsheet-python-sdk`, `cryptography` |
| `imagetocell_usage_log` UNIQUE constraint | ✅ Migration written (SH-98 PR #144 in review) | DB-level idempotency for billing | Supabase |
| Hotfix webhook payload validation (SH-104) | ✅ Merged (PR #142) | Stop crashing on unknown objectTypes | — |
| ElastiCache Valkey | ⏳ Provisioning (SH-10 spike in progress) | Shared runtime: cache, dedup, locks, rate limit, streams | AWS account access |
| Redis-backed dedup in webhook_handler (SH-99) | 🔒 Blocked on ElastiCache | Cross-process dedup, survives ECS task restart | smarthub_core, ElastiCache |
| Smartsheet client async migration (SH-100..102) | 🔒 Blocked on ElastiCache | Cluster-wide rate limit + retry; async-first stack | smarthub_core, ElastiCache |
| Bolt cap atomic deduct (SH-103) | 🔒 Blocked on ElastiCache + SH-99 | Eliminate TOCTOU in cap enforcement | smarthub_core, ElastiCache |
| Webhook Gateway (polyglot) | 🔒 Phase 3 — future | Single ingress for all providers, signature verification, normalization, XADD to stream | smarthub_core, ECS, ALB, ElastiCache |
| Worker stream consumers | 🔒 Phase 3.5 — future | XREADGROUP, dedup, retry, DLQ, ACK-then-publish | smarthub_core, ElastiCache, Webhook Gateway |
| `platform-events` cross-tool bus | 🔒 Phase 5 — future | Decouple Bulk Uploader from i2c | smarthub_core, Webhook Gateway |
| Lifecycle Manager (centralizado) | 🔒 Phase 3 — future | Singleton webhook registration/cleanup across all tools and providers | smarthub_core, ElastiCache |

### 3.2 Diagramas

**Estado actual (legacy, antes de Pulse completo):**

```
                  Smartsheet
                       │ (webhooks per-sheet)
        ┌──────────────┼──────────────┐
        │              │              │
   ┌────▼────┐    ┌────▼────┐    ┌────▼────┐
   │  i2c-3  │    │ cell-   │    │ atch-   │     ...  (8 backends)
   │ recv +  │    │ history │    │ counter │
   │ process │    │ poller  │    │ poller  │
   └────┬────┘    └────┬────┘    └────┬────┘
        │              │              │
        └──────────────┼──────────────┘
                       │
                  ┌────▼────┐
                  │ Supabase│
                  │  (SoT)  │
                  └─────────┘
```

Cada backend duplica: crypto, supabase wrapper, smartsheet client, webhook receiver, lifecycle manager. Sin layer compartido.

**Estado objetivo Pulse:**

```
                  Smartsheet     HubSpot     Slack    Stripe ...
                       │            │           │         │
                       └────────────┴───────────┴─────────┘
                                    │
                       ┌────────────▼─────────────┐
                       │   Webhook Gateway        │
                       │   (polyglot, compartido) │
                       │   - validate signature   │
                       │   - challenge response   │
                       │   - normalize envelope   │
                       │   - XADD stream:{tool}   │
                       └────────────┬─────────────┘
                                    │
                              ┌─────▼──────┐
                              │   Redis    │
                              │ ElastiCache│
                              │  Valkey    │
                              │            │
                              │  streams:  │
                              │  - i2c     │
                              │  - bau     │
                              │  - ch      │
                              │  - ...     │
                              │  - dlq:*   │
                              │  - platform-events
                              │            │
                              │  cache/    │
                              │  dedup/    │
                              │  locks/    │
                              │  ratelim   │
                              └─────┬──────┘
                                    │
            ┌───────────────┬───────┼───────┬─────────────┐
            │               │       │       │             │
       ┌────▼────┐     ┌────▼────┐  │  ┌────▼────┐   ┌────▼────┐
       │ worker  │     │ worker  │  │  │ worker  │   │  ...    │
       │ i2c-3   │     │ bau     │  │  │ ch      │   │ 8 tools │
       │ (async) │     │ (async) │  │  │ (async) │   │         │
       └────┬────┘     └────┬────┘  │  └────┬────┘   └─────────┘
            │               │       │       │
            └───────────────┴───────┼───────┘
                                    │
                       ┌────────────▼─────────────┐
                       │      Supabase            │
                       │  Source of truth         │
                       │  - imagetocell_usage_log │
                       │  - configs               │
                       │  - tokens                │
                       │  - subscriptions         │
                       └──────────────────────────┘

       Sidecar (singleton vía Redis lock):
       ┌─────────────────────────────────────┐
       │  Lifecycle Manager                  │
       │  - register/renew/cleanup webhooks  │
       │  - per provider, per tool, per org  │
       └─────────────────────────────────────┘
```

### 3.3 Lo que explícitamente NO incluimos

Decisiones de scope:

| Componente | Por qué NO |
|---|---|
| Service mesh / sidecars (Istio, Linkerd) | Overkill a nuestra escala (<10 services). Latencia y complejidad operacional no se justifican. |
| Multi-región | Single region eu-west-2 alcanza. Smartsheet también es single-tenant para nosotros. |
| Event sourcing | Postgres es la source of truth tradicional. Event sourcing introduce complejidad de proyecciones que no necesitamos. |
| CQRS | Nuestras queries son OLTP simples — un solo modelo es suficiente. |
| Kubernetes | ECS Fargate cumple. K8s agregaría complejidad sin beneficio. |
| Redis Cluster mode con sharding | Single-shard hasta 200+ orgs activas. Sharding agrega complejidad operacional grande. |
| MemoryDB for Redis | Durabilidad estricta no es requerimiento (ver Sección 5 sobre trade-offs). |
| Kafka / RabbitMQ | Redis Streams cubre nuestros casos sin operar un broker adicional. |
| OAuth refresh tokens en plaintext en Redis | Tokens siempre ciphertext en Redis, decrypt solo in-process. |
| GraphQL gateway | REST/PostgREST cubre las queries del frontend. |

---

## 4. Anti-patrones en la arquitectura ACTUAL (que queremos escapar)

Esta es la lista honesta de lo que hoy duele, por orden de severidad. Cada uno se cierra explícitamente en Pulse.

### 4.1 Code Duplication Across Backends (Severidad: ALTA)

`crypto_helper.py` existe duplicado en 6 backends. `SmartsheetService` está reescrita en cada uno con sutiles diferencias. Bug fixes en uno no se propagan a los otros automáticamente.

Visible en SH-9 audit: 6 versiones de crypto, 4 implementaciones distintas de retry de Smartsheet, 3 patterns distintos de pool de Supabase.

**Cómo cierra Pulse:** `smarthub_core` es la única implementación. Cada backend importa desde la librería. Bug fixes se propagan a todos los consumers vía version bump.

### 4.2 Strict Schema Validation on External API Inputs (Severidad: CRÍTICA — causó incident)

`WebhookEvent.objectType` era un Pydantic enum estricto con 5 valores. Cualquier `objectType` fuera del enum (`discussion`, `proof`, `reminder`, ...) hacía que el **payload entero se rechazara** — incluyendo los attachments válidos del mismo batch.

Producción incident 2026-05-15: cliente perdió 9 attachments porque Smartsheet incluyó un `discussion` event en un batch con `attachment` events legítimos.

**Cómo cierra Pulse:** principio "Tolerant Inputs, Strict Logic" (Sección 2.8). Cerrado en SH-104.

### 4.3 In-Memory Dedup That Doesn't Survive Restart (Severidad: ALTA)

`webhook_handler._processing_cache`, `_completed_attachments`, `_row_cache` son `dict()` Python en memoria. Cuando un ECS task se reinicia o cuando Smartsheet rutea un retry a un task diferente, **no hay dedup**. Combinado con la falta de UNIQUE constraint en DB → double-bill.

**Cómo cierra Pulse:** SH-99 migra los 3 caches a Redis con `SET NX EX 600`. Sobrevive restart, cross-task. SH-98 es la red de seguridad en DB.

### 4.4 Per-Process Rate Limit (Severidad: MEDIA-ALTA)

Cada uvicorn worker mantiene su propio token bucket de Smartsheet. 4 workers × 275/min = 1100/min cluster-wide. Smartsheet real limit: 300/min. **Cluster overshoot → 429s → retries → bolt cap drift**.

**Cómo cierra Pulse:** SH-100 migra a `smarthub_core.platforms.smartsheet.SmartsheetClient` que usa una Lua sliding window centralizada en Redis. Una sola fuente de verdad por `(org, product_slug)`.

### 4.5 `time.sleep()` in Async Retry Path (Severidad: MEDIA)

Versión legacy del `SupabaseService.execute_with_retry_async` usaba `time.sleep` durante backoff. Eso **bloquea el event loop** y mantiene la conexión httpx ocupada durante la espera, agotando el pool más rápido.

**Cómo cierra Pulse:** ya cerrado en SH-14 (PR #139). `smarthub_core.db.supabase` usa `asyncio.sleep` y libera la conexión al pool durante el backoff.

### 4.6 Unbounded httpx Pool (Severidad: MEDIA — causaba incidents)

`supabase-py` default es `max_connections=100` por cliente. 4 workers × 100 = 400 conexiones por backend × 8 backends = 3200 max contra Supabase compute micro que aguanta ~50.

Síntoma: `Failed to log image conversion: RemoteProtocolError: Server disconnected` en logs de producción.

**Cómo cierra Pulse:** ya cerrado en SH-14. `smarthub_core.db.supabase.SupabaseService` configura `httpx.Limits(max_connections=SUPABASE_MAX_CONNECTIONS)` con default 5. Calibrable per-environment.

### 4.7 Polling para Datos Real-Time (Severidad: BAJA, cost)

Cell-History (30s), Attachment-Counter (70s), Dynamic-DDL (90s) hacen polling cada N segundos contra Smartsheet API. Aun cuando no hay cambios. Consume rate-limit budget y dinero AWS.

**Cómo cierra Pulse (Phase 4):** migrar los polling-based tools a webhook-based, consumiendo de `stream:cell-history` etc. Phase 4, sin urgencia hasta que el cliente lo pida o el costo lo justifique.

### 4.8 Acoplamiento Cross-Tool Implícito (Severidad: MEDIA)

Bulk Uploader necesita que i2c esté procesando. Hoy se entera por mirar `imagetocell_usage_log` directamente (acoplamiento implícito vía DB). Si i2c cambia el shape del log, BU se rompe.

**Cómo cierra Pulse (Phase 5):** `stream:platform-events` es el contrato explícito. i2c publica `image_processed`, BU suscribe. Cambios en la implementación interna de i2c no afectan a BU mientras el envelope del evento se respete.

### 4.9 ECS Service per Backend, Sin Layer Compartido (Severidad: BAJA pero crece con # tools)

8 ECS services hoy. Cada uno tiene su propia task definition, su propia security group, su propia ALB target group, su propia métrica de salud. Onboarding de un tool 9° significa replicar todo eso.

**Cómo cierra Pulse:** mismo número de services, pero ahora **comparten** smarthub_core (lib), Redis (runtime), gateway (ingress). Tres componentes nuevos a operar, pero el delta de complejidad por nuevo tool baja sustancialmente.

### 4.10 Falta de Observabilidad Estructurada Cross-Tool (Severidad: MEDIA)

Cada backend loguea con prefijos distintos (`[WEBHOOK:INCOMING]`, `[I2C]`, ...). Correlation IDs no existen — debuggear un webhook que tocó 3 tools requiere grep por timestamps.

**Cómo cierra Pulse:** correlation_id generado en el gateway y propagado al envelope del stream. Todos los workers loguean con el mismo ID. (Phase 6 — observability layer, anytime).

---

## 5. Anti-patrones que ACEPTAMOS en Pulse (trade-offs honestos)

Pulse no es perfecto. Esta sección lista los costos arquitectónicos que aceptamos a sabiendas, en orden de impacto.

### 5.1 Complejidad Operacional Aumentada (ACEPTADO)

Antes: 8 servicios stateless, fácil de razonar. Ahora: 8 services + Redis cluster + (futuro) gateway + streams + Lifecycle Manager singleton.

**Trade-off.** Pagamos complejidad operacional a cambio de escalabilidad y plug-and-play multi-provider. **Justificado** porque el negocio crece 5-8 tools/año.

**Mitigación.** ElastiCache es managed (no operamos Redis); el gateway es un único service nuevo; los workers ya existen, solo cambian su trigger.

### 5.2 Eventual Consistency en Cache-Aside (ACEPTADO)

`org-config` cachea por 60s. Si admin UI cambia un config, el worker que cachee la versión vieja la usa por hasta 60s más. Para un cambio de config no-crítico (ej. renombrar la columna de salida), es aceptable.

**Trade-off.** Pagamos un poco de stale data por bajar carga a Postgres.

**Mitigación.** TTL corto (60s) y `DEL cache key` en el path de write hace que la inconsistencia sea típicamente <1s. Para configs críticas que requieren consistencia estricta, el admin UI podría flush la cache via API explícito (no implementado, no se justifica todavía).

### 5.3 Single-Point-of-Failure: Redis (ACEPTADO en lean spec)

Si Redis cae:
- Cache: cae todo a Postgres (latencia ↑)
- Dedup cross-task: cae a in-memory per-process (mismo nivel que hoy)
- Rate limit: cae a per-process (mismo nivel que hoy)
- Locks: expiran solos, behavior degradado pero no roto
- **Streams: pierde mensajes nuevos que XADD durante el outage** — Smartsheet retries cubren la mayoría

**Trade-off.** Aceptamos que Redis-down es un degradation event. No es zero-downtime.

**Mitigación.** Multi-AZ con replica si el negocio lo justifica (+$23/mo). Hoy single-node prod es lean defendible. ElastiCache process restart es automático, ~30-60s.

### 5.4 No-Outbox para platform-events (ACEPTADO temporalmente)

Si worker hace `XACK` y muere antes de `XADD stream:platform-events`, perdemos el evento. Bulk Uploader no se entera.

**Trade-off.** Pagamos riesgo de event loss por simplicidad de implementación.

**Mitigación.** BU tiene su propio retry/reconcile. Cuando lleguen tools sin retry propio, implementamos outbox transaccional.

### 5.5 Dedup TTL Window de 10 Minutos (ACEPTADO)

Si Redis crashea y vuelve, los dedup keys con TTL 600s pueden expirar y otro worker reprocesar el mismo attachment. Mitigado por SH-98 UNIQUE constraint a nivel DB.

**Trade-off.** Aceptamos que dedup sea "best effort en una ventana de 10 minutos" en lugar de "permanente forever".

**Mitigación.** Defense in depth — UNIQUE constraint en DB es la verdadera safety net.

### 5.6 Coupling a Redis (ACEPTADO)

Hoy nuestro código no requiere Redis. Después de la migración, varias capas dependen de él reachable. Local dev requiere `docker run smarthub-redis`.

**Trade-off.** Pagamos un setup overhead local por capacidades cluster-wide.

**Mitigación.** Docker Redis es 1 comando. CI usa el mismo container. Producción tiene ElastiCache managed. Cada uso de Redis tiene fallback documentado para Redis-down.

### 5.7 Lock-in a Patrones Smartsheet-céntricos (ACEPTADO, mitigado)

Aunque diseñamos polyglot, muchas decisiones tienen sabor Smartsheet (scope=sheet, webhook batching window de 30s, etc.).

**Trade-off.** Pagamos potencial refactor en el futuro si un proveedor tiene un modelo muy distinto.

**Mitigación.** Las abstracciones en `platforms/shared/` (TokenResolver, BaseWebhookHandler) son honestamente abstractas. Si Stripe necesita algo radicalmente distinto, agregaremos métodos opcionales sin romper Smartsheet.

### 5.8 Stream Memory bounded por MAXLEN (ACEPTADO)

Convención: `XADD stream:{tool} MAXLEN ~ 10000 * data`. Si publicamos más de 10k mensajes por tool y los consumers no consumen, **los más viejos se descartan**.

**Trade-off.** Pagamos potencial mensaje loss bajo backlog masivo por memoria bounded.

**Mitigación.** 10000 mensajes es ~16MB por stream. Si los workers caen consistentemente, el problema real es la salud de los workers, no la stream cap.

---

## 6. Trade-offs vs arquitectura actual — tabla resumen

| Dimensión | Arquitectura actual | Pulse | Delta |
|---|---|---|---|
| **Latencia request individual** | Direct API, no Redis hops | +0.5-2ms por Redis op (cache lookup, dedup check) | Pagamos ~ms por correctness |
| **Throughput máximo cluster** | 4 uvicorn × 8 tools = ~32 procesos | Misma cantidad de procesos, pero con coordinación cross-cluster | Sin cambio en throughput; cambio en eficiencia (no más 429 cascades) |
| **Costo AWS mensual prod (i2c-3 only)** | ~$475/mo | ~$520/mo (+$45 ElastiCache) | +9.5% |
| **Tiempo onboarding nuevo tool** | 2-4 semanas (replicar webhook receiver, crypto, etc.) | 1-2 semanas (módulo en platforms/, register consumer) | -50% time-to-market |
| **Failure isolation** | Si una tool tiene bug, solo esa tool cae | Mismo (per-tool bulkhead preserved) | Sin cambio |
| **Single-point failure** | Postgres (Supabase) | Postgres + Redis | +1 SPOF, mitigado por degradación graceful |
| **Debugging complexity** | Logs en un service por tool | Cross-service correlation con correlation_id | Inicialmente más difícil, más visibilidad después |
| **Testing infrastructure** | Tests unitarios alcanzan | Necesita Redis local para integration tests | Pagamos overhead test infra |
| **Operational know-how requerido** | FastAPI + Postgres | + Redis + Streams + ElastiCache | Curva de aprendizaje |
| **Billing correctness** | Riesgo cross-task double-bill (mitigado por SH-104, cerrado por SH-98) | Cerrado a nivel DB + Redis + worker | Strictly mejor |
| **Multi-instance scaling** | 1 ECS task per service (vertical only) | N tasks, cluster-wide coordination | Habilita scaling horizontal |
| **Cross-tool integration** | Acoplamiento implícito vía DB queries | `stream:platform-events` con envelope explícito | Strictly mejor |
| **Provider plug-and-play** | Cada provider requiere mucho código duplicado | `platforms/{provider}/` con interfaces abstractas | Strictly mejor |

---

## 7. Proceso de migración — Strangler Fig en detalle

### 7.1 ¿Por qué Strangler Fig y no otros patterns?

Alternativas que evaluamos y rechazamos:

| Alternativa | Por qué NO |
|---|---|
| **Big-bang rewrite** | Bloquea capacidad por meses. Sin rollback granular. Riesgo billing inaceptable. |
| **Branch by Abstraction** | Útil dentro de un service, no entre 8. La abstracción tendría que vivir en `smarthub_core` igual — que ES el approach Strangler Fig. |
| **Forklift / Lift-and-shift** | No aplica, no estamos moviendo plataforma. |
| **Parallel Run con shadow traffic** | Sería ideal para validar correctness pero requiere infra extra (router que duplica tráfico). Overkill a nuestra escala. |
| **Feature flags fine-grained** | No es alternativo a Strangler, sino complementario. Lo usamos donde se justifica. |

Strangler Fig fue elegido porque:

- Cada paso es **independientemente revertible** (cada PR es atómico)
- Permite **coexistencia** del legacy con el nuevo durante semanas/meses
- Cierra **un módulo a la vez por backend**, en lugar de un backend completo
- Equipo puede hacer **otras prioridades** entre migraciones — no es exclusivo

### 7.2 Cómo lo aplicamos concretamente

#### Capa 1 — la librería compartida `smarthub_core`

**Construcción.** Se hizo en un branch dedicado (SH-9), aislado, sin consumidores. Se merged a develop como código "muerto" (presente pero no importado por nadie). Tests propios pasaron. Riesgo de breakage = cero porque nada lo usaba.

#### Capa 2 — migración módulo por módulo dentro de un backend

**Ejemplo concreto: i2c-3.**

- **SH-14 Phase 1.2 (crypto)** — el módulo más chico, una sola línea de import (`from src.services.crypto import decrypt_token`). Cambio quirúrgico, validación contra un token real de staging, tests existentes siguen pasando. PR mergeado. Rollback = un revert.
- **SH-14 Phase 1.3 (supabase)** — más sitios de uso (8+ files), pero la API pública del legacy `SupabaseService` se preserva 1:1. El cambio es: `src/services/supabase/client.py` ahora es un **shim de ~70 líneas** que re-exporta el class de `smarthub_core` + mantiene el singleton `get_supabase_service()`. Cero cambios en consumidores. Comportamiento mejorado (pool bounded, retry async correcto, retryable patterns incluyen HTTP/2 GOAWAY).
- **SH-100..103 (smartsheet client)** — el más complejo porque la API legacy es sync y la nueva es async. Splittea en 3 sub-tickets (wrappers → core → workers) cada uno bajo 8 SP. Bloqueado hoy por ElastiCache.

**Patrón general en cada migración:**

1. Identificar el módulo y sus consumers
2. Verificar API compat (igual surface entre legacy y nuevo, o construir shim)
3. Cambiar imports en consumers (o reemplazar el módulo legacy por shim)
4. Tests existentes deben seguir pasando sin cambios significativos
5. Smoke test con datos reales (token real, payload real, sheet real)
6. Container build + integration validation
7. Merge, deploy a staging, observación 1 semana, deploy a prod

#### Capa 3 — coexistencia y eventual decommission

Durante el período de transición (varios meses), conviven:
- `src/services/crypto.py` legacy (en backends no-migrados) y `smarthub_core.crypto` (en i2c-3)
- `src/services/supabase/client.py` como shim sobre smarthub_core
- `SmartsheetService` legacy (en uso) y `SmartsheetClient` (existente en smarthub_core, pendiente de adopción)

**Cuando todos los consumers han migrado**, el código legacy se borra con un grep:

```bash
grep -r "from src.services.crypto import" backend/  # if zero hits, safe to delete
```

#### Capa 4 — el Webhook Gateway (futuro Phase 3)

Aún más Strangler. Cuando el Gateway exista:

1. Se despliega como un service nuevo, sin tráfico (URL distinta de los receivers per-tool actuales)
2. Por **un solo tool y una sola sheet experimental**, re-registramos el webhook a la URL del gateway
3. Validamos que el gateway → stream → worker flow procesa correctamente
4. Migramos webhook por webhook (script de migration)
5. Cuando todos los webhooks de un tool están en el gateway, eliminamos el receiver de ese tool
6. Repetimos por tool hasta que el último receiver per-tool se elimine

### 7.3 Estado actual de la migración (snapshot)

```
Capa            | i2c-3 | bau | ch  | atch | bspc | ddl | textnow | id  | ssfeat | enc
----------------|-------|-----|-----|------|------|-----|---------|-----|--------|-----
crypto          |  ✅   | ⏳  | ⏳  | ⏳   | ⏳   | ⏳  | ⏳      | ⏳  | ⏳     | ⏳
supabase        |  ✅   | ⏳  | ⏳  | ⏳   | ⏳   | ⏳  | ⏳      | ⏳  | ⏳     | ⏳
smartsheet      |  🔒   | ⏳  | ⏳  | ⏳   | ⏳   | ⏳  | n/a     | ⏳  | n/a    | n/a
worker base     |  🔒   | ⏳  | ⏳  | ⏳   | ⏳   | ⏳  | ⏳      | ⏳  | ⏳     | ⏳
gateway-routed  |  🔒   | 🔒  | 🔒  | 🔒   | 🔒   | 🔒  | 🔒      | 🔒  | 🔒     | 🔒

✅ migrated   ⏳ pending   🔒 blocked (waiting for ElastiCache or prior phase)
```

Estado conservador en el resto de backends es intencional — primero validamos completamente en i2c-3 antes de replicar.

### 7.4 Cómo saber cuándo el Strangler terminó

El árbol viejo está completamente estrangulado cuando:

1. **Cero imports** de `src.services.crypto`, `src.services.supabase`, `src.services.smartsheet` en todo el repo
2. **Cero webhook receivers** per-tool, solo el Webhook Gateway
3. **`smarthub_core`** es la única fuente de los patterns críticos (crypto, db, smartsheet client, worker base)
4. **Polling loops** (cell-history, attachment-counter) eliminados o documentados como aceptables
5. **`stream:platform-events`** tiene al menos 2 consumers reales (i2c publica + BU consume)

A ese momento, el código legacy en `src/services/{crypto,supabase,smartsheet}/` se borra del repo. La operación de eliminar = un PR de cleanup.

---

## 8. Decisiones que reevaluaremos

Algunas decisiones tienen una **fecha de revisión implícita**. Esta sección las hace explícitas para que en 6-12 meses el equipo no las herede como dogma.

| Decisión actual | Cuándo reevaluar | Posible cambio |
|---|---|---|
| Single-node ElastiCache prod (no Multi-AZ) | Después de 3 incidents de Redis-down customer-visible en 6 meses | Subir a Multi-AZ + replica (+$23/mo) |
| `maxmemory-policy: volatile-lru` | Si vemos que streams pierden mensajes por eviction | Investigar memoria, posiblemente subir node size |
| MAXLEN ~10000 por stream | Si workers caen consistentemente y backlog crece | Levantar MAXLEN o agregar más workers |
| ElastiCache vs MemoryDB | Si un tool sufre customer-visible loss attribuible a Redis-down | MemoryDB (+3x costo) |
| No outbox para platform-events | Si un tool sin retry-propio se vuelve consumer | Implementar outbox transaccional |
| `cache.t4g.small` prod | Cuando consistentemente > 60% memoria o CPU | Subir a `cache.t4g.medium` |
| Sin cluster mode (sharding) | Cuando > 200 orgs activas o > 5000 webhooks/min | Activar cluster mode |
| Polling para cell-history / attachment-counter | Si latencia 30s/70s es customer-reportada | Migrar a webhooks con stream consumers |
| 8 ECS services separados | Si time-to-deploy se vuelve un problema | Consolidar en 1-2 services con workers internos |

---

## 9. Resumen ejecutivo

**Pulse** = librería compartida (`smarthub_core`) + ElastiCache Valkey + Webhook Gateway polyglot + Redis Streams + idempotencia multi-capa, todo construido para soportar 5-8 nuevos tools por año con múltiples providers externos.

**Patrones load-bearing.** Cache-Aside (Postgres = SoT), Polyglot Adapters por proveedor, Distributed Singleton con lock, Bounded Concurrency, Event-driven Streams, Idempotency multi-layer, Tolerant Inputs / Strict Logic, Bulkhead per-tool.

**Anti-patterns que escapamos del legacy.** Code duplication × 6, strict input validation que crashea, in-memory dedup que no sobrevive restart, per-process rate limit que sobrepasa el cap real de Smartsheet, `time.sleep` en async, httpx pool sin bounds, polling para datos real-time, cross-tool coupling implícito vía DB, sin observabilidad estructurada cross-tool.

**Anti-patterns que aceptamos.** Complejidad operacional ↑, eventual consistency en cache, Redis SPOF (mitigado por degradation graceful), no-outbox para platform-events (deferido), dedup TTL ventana de 10 min (mitigado por UNIQUE constraint).

**Migration approach = Strangler Fig.** Construir nuevo al lado del viejo, migrar módulo por módulo y backend por backend, coexistir durante meses, decommission del legacy cuando el último consumer migre. Cada paso individualmente revertible. Phase 0 (smarthub_core library) ✅. Phase 1 (i2c-3 migración) en progreso. Phase 2-5 condicionales según métricas.

**No es perfecto, pero es honesto.** Pagamos +$45/mo, +complejidad operacional, +1 SPOF a cambio de escalabilidad, plug-and-play multi-provider, billing correctness arquitectónica, y time-to-market 50% menor para nuevos tools.
