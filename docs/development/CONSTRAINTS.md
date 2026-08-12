# Restricciones del Proyecto

Cosas que NO se deben hacer sin el protocolo indicado. Cada restricción existe por una razón documentada.

---

## NUNCA hacer sin coordinación total

### 1. Cambiar APP_CRYPTO_KEY

**Impacto:** Los 8 backends fallan simultáneamente. Todos los tokens de Smartsheet en DB se vuelven ilegibles.

**Protocolo de rotación (si es absolutamente necesario):**
1. Crear nueva key `APP_CRYPTO_KEY_NEW`
2. Escribir script de migración que reencripte todos los tokens en `smartsheet_tokens` con la nueva key
3. Deployar TODOS los backends con AMBAS keys (leer con vieja, reencriptar con nueva)
4. Ejecutar script de migración
5. Verificar que todos los tokens son legibles
6. Remover la key vieja de todos los backends
7. Deployar TODOS los backends de nuevo

**Tiempo mínimo:** 2 horas de ventana de mantenimiento.

### 2. Cambiar schema de `smartsheet_tokens`

**Impacto:** Los 8 backends no pueden leer credenciales.

**Protocolo:**
1. Agregar columnas nuevas como NULLABLE (no renombrar ni eliminar existentes)
2. Deployar backends que usan las nuevas columnas
3. Ejecutar migración de datos
4. Solo después de verificar: eliminar columnas obsoletas

### 3. Cambiar schema de `user_profiles`

**Impacto:** Los 8 backends pierden capacidad de identificar la organización del usuario.

**Protocolo:** Igual que `smartsheet_tokens`. Siempre additive-first.

### 4. Cambiar RPCs de billing

RPCs afectadas: `get_active_subscriptions()`, `get_user_active_plan()`

**Impacto:** 6 backends no pueden verificar límites de plan.

**Protocolo:** Crear nueva versión de la RPC (`get_active_subscriptions_v2`), migrar backends uno por uno, luego deprecar la vieja.

---

## Non-negotiable rules (apply everywhere)

### All code must be written in English

Variable names, function names, class names, comments, docstrings, log messages, exception messages, SQL identifiers, and commit messages — all in English. No exceptions.

```python
# WRONG
async def procesar_imagen(ruta: str) -> bytes:
    # convierte la imagen
    ...

# CORRECT
async def process_image(file_path: str) -> bytes:
    # convert image to the required output format
    ...
```

### Every Supabase and Smartsheet call must have retry logic

No bare `.execute()` calls without retry. No direct Smartsheet SDK calls without `call_smartsheet_with_retry()`.

```python
# WRONG
result = db.table("configs").select("*").execute()  # ❌ no retry

# CORRECT
result = db.execute_with_retry(
    lambda: db.table("configs").select("*").eq("org_id", org_id).execute(),
    "get active configs"
)  # ✅
```

### Every feature must be testable

- Inject all external dependencies (db, redis, smartsheet client) — never instantiate inside business logic.
- Tests must be able to run without real Supabase, Redis, or Smartsheet connections.
- Maintain `tests/` folder in every backend with unit and integration tests.
- New features require at least one test covering the happy path and one covering the error path.

### Log every important event and every error

- Every job start, completion, and failure must be logged.
- Every external API call failure must be logged with status code and context.
- Never swallow exceptions silently (`except Exception: pass` is forbidden).
- Always include `exc_info=True` on ERROR level logs to capture the stack trace.

---

## NUNCA hacer en ningún contexto

### No usar time.sleep() en código async

```python
# MAL — bloquea el event loop de FastAPI
async def register_webhook():
    time.sleep(2)  # ❌

# BIEN
async def register_webhook():
    await asyncio.sleep(2)  # ✅
```

**Por qué:** `time.sleep()` en async bloquea FastAPI y puede causar timeout en webhook challenges de Smartsheet (30s).

### No usar INTEGER para IDs de Smartsheet

```python
# MAL — overflow con IDs reales de Smartsheet
worksheet_id: int  # ❌ en Python ok, pero en DB PostgreSQL INTEGER overflow

# BIEN
worksheet_id: int  # ✅ en Python (64-bit nativo)
# En DB: BIGINT o TEXT
```

### No hacer llamadas bloqueantes en Depends() de FastAPI para rutas de webhook

```python
# MAL — FastAPI resuelve TODOS los Depends antes de ejecutar el handler
# Si la consulta tarda, el challenge de Smartsheet expira
@router.post("/webhook/smartsheet")
async def handle_webhook(
    db: SupabaseService = Depends(get_db),
    config: Config = Depends(get_config_heavy),  # ❌ puede tardar 5s
):
    if challenge := request.headers.get("Smartsheet-Hook-Challenge"):
        return {"smartsheetHookResponse": challenge}  # ya es tarde

# BIEN — challenge primero, antes de cualquier Depends pesado
@router.post("/webhook/smartsheet")
async def handle_webhook(request: Request):
    if challenge := request.headers.get("Smartsheet-Hook-Challenge"):
        return JSONResponse(
            {"smartsheetHookResponse": challenge},
            headers={"Smartsheet-Hook-Response": challenge}
        )
    # Solo aquí hacer consultas pesadas
    await process_webhook(request)
```

### No llamar a Smartsheet API sin Change-Agent header en rutas que modifican datos

```python
# MAL — puede causar loops infinitos con webhooks
sheet.update_rows(sheet_id, [row])  # ❌

# BIEN
sheet.update_rows(
    sheet_id,
    [row],
    headers={"Smartsheet-Change-Agent": "smarthub-i2c"}  # ✅
)
```

### No almacenar column_ids o row_ids como INTEGER en PostgreSQL

Smartsheet usa IDs de 64 bits. PostgreSQL INTEGER es 32 bits → overflow silencioso.

```sql
-- MAL
worksheet_id INTEGER  -- ❌

-- BIEN
worksheet_id BIGINT   -- ✅
-- o
worksheet_id TEXT     -- ✅ si se necesita serialización JSON
```

### No modificar las carpetas legacy

```
backend/image-to-cell/        → NO TOCAR
backend/image-to-cell_2/      → NO TOCAR
backend/image-to-cell-donotuse/ → NO TOCAR
```

Están desactivados en producción pero el código existe para referencia histórica.

---

## Restricciones por componente

### image-to-cell-3

- `processing_mode = "webhook"` en producción. `"webhook_v3"` solo para testing.
- El lifecycle manager usa `WEBHOOK_PROCESSING_MODES` env var para saber qué configs gestionar.
- No agregar lógica de procesamiento de imágenes en el webhook handler — solo encolar.
- No cambiar el formato del payload que se publica a `stream:platform-events`.

### crypto_helper.py (en todos los backends)

- Si corriges un bug, corregirlo en los 6 archivos que lo tienen copiado.
- El algoritmo debe ser AES-GCM con IV de 12 bytes y TAG de 16 bytes.
- El formato de token es: `base64(IV || TAG || CIPHERTEXT)`.
- No cambiar este formato sin migrar todos los tokens en DB.

### Puertos de backend

Los puertos están hardcodeados en el frontend. Cambiar un puerto sin actualizar el frontend rompe esa herramienta para todos los usuarios.

| Backend | Puerto | Var de entorno frontend |
|---|---|---|
| encrypt-tool | 3051 | REACT_APP_ENCRYPT_URL |
| id-checker | 3002 | REACT_APP_ID_CHECKER_BACKEND_URL |
| image-to-cell-3 | 3011 | REACT_APP_I2C_BACKEND_URL |
| bulk-attachment-uploader | 3003 | REACT_APP_BAU_BACKEND_URL |
| cell-history | 3014 | REACT_APP_CH_BACKEND_URL |
| attachment-counter | 3005 | REACT_APP_AC_API_BACKEND_URL |
| dynamic-ddl | 3007 | REACT_APP_DDL_BACKEND_URL |
| textnow | 3131 | (hardcoded en frontend) |

### Supabase RLS

- Todos los backends usan `SUPABASE_SERVICE_ROLE_KEY` que bypasea RLS.
- Las políticas RLS protegen el acceso desde el frontend (con JWT de usuario).
- No deshabilitar RLS en tablas que el frontend consulta directamente.
- Si agregas una tabla nueva que el frontend consulta, crear políticas RLS.

---

## Rate limits de Smartsheet API

La cuenta tiene un límite global de **300 requests/minuto**.

Con múltiples backends corriendo simultáneamente, el límite se comparte:

```
attachment-counter:  240 req/min configurados
cell-history:        ~150 req/min estimados (30s polling × configs)
dynamic-ddl:         240 req/min configurados
image-to-cell-3:     275 req/min configurados

TOTAL POSIBLE: >900 req/min → excede el límite de 300
```

**Restricción actual:** Los backends no coordinan el rate limit entre sí.
**Solución futura:** Rate limiter global en Redis (parte de `smarthub_core`).
**Mientras tanto:** No reducir los polling intervals por debajo de los valores actuales.
