# Mapa de Dependencias — Smarthub

Leer este documento antes de modificar cualquier tabla, variable de entorno, o archivo compartido.

---

## Dependencias que rompen TODO simultáneamente

Si cambias cualquiera de estas tres cosas sin coordinación, los 8 backends fallan al mismo tiempo:

```
APP_CRYPTO_KEY
  → Todos los backends usan esta clave AES-GCM para descifrar tokens
  → Si cambia, ningún backend puede leer credenciales de Smartsheet
  → No hay migración gradual posible — es todo o nada
  → Protocolo: ver CONSTRAINTS.md#rotacion-de-crypto-key

smartsheet_tokens (tabla de Supabase)
  → Almacena todos los tokens encriptados de todos los usuarios
  → Todos los backends leen de aquí para autenticarse con Smartsheet
  → Cambiar schema = todos los backends fallan
  → Nunca renombrar columnas sin migración coordinada

user_profiles (tabla de Supabase)
  → Todos los backends la consultan para obtener organization_id del usuario
  → Sin organization_id, ningún backend puede hacer RLS en Supabase
  → Cambiar schema = todos los backends fallan
```

---

## Dependencias por grupos

### 8 backends → leen estas tablas

| Tabla | Tipo de acceso | Qué pasa si cambia |
|---|---|---|
| `user_profiles` | READ | Todo falla |
| `smartsheet_tokens` | READ | Todo falla |
| `organization_ss_api_base` | READ | Todo falla (no pueden determinar URL regional) |
| `organizations` | READ | Degradación parcial |

### 6 backends → billing y límites de plan

Afecta a: I2C, Cell History, Attachment Counter, Bulk Uploader, I2C v2 (legacy), I2C v1 (legacy)

| Tabla / RPC | Tipo | Qué pasa si cambia |
|---|---|---|
| `organization_subscriptions` | READ | Los 6 backends pierden control de billing |
| RPC `get_active_subscriptions(p_organization_id)` | CALL | Los 6 backends fallan al verificar límites |
| RPC `get_user_active_plan(p_user_id)` | CALL | I2C y Cell History fallan |

### Dependencia implícita I2C → Bulk Uploader

```
I2C registra webhooks en Smartsheet para un sheet.
Bulk Uploader usa esos mismos webhooks como trigger.

Si I2C deja de registrar webhooks:
  → Smartsheet deja de notificar cambios al sheet
  → Bulk Uploader no recibe triggers de procesamiento

Esta dependencia NO está en el código — está en la configuración de Smartsheet.
Documentarla aquí para no olvidarla al migrar.
```

---

## Tabla de impacto: "¿Qué pasa si cambio X?"

| Cambias | Rompe | No afecta |
|---|---|---|
| `APP_CRYPTO_KEY` | **TODOS** (8 backends) | — |
| `smartsheet_tokens` schema | **TODOS** (8 backends) | — |
| `user_profiles` schema | **TODOS** (8 backends) | — |
| `organization_ss_api_base` | **TODOS** (llaman Smartsheet) | Password Reset, SSFeatures |
| `organization_subscriptions` schema | I2C, Cell History, Att. Counter, Bulk Uploader | DDL, TextNow, Contact Book |
| RPC `get_active_subscriptions` | I2C, Cell History, Att. Counter, Bulk Uploader | DDL, TextNow, Contact Book |
| `imagetocell_config` schema | Solo I2C v3 | Todo lo demás |
| `imagetocell_webhooks` schema | Solo I2C v3 | Todo lo demás |
| `attachment_counter_*` tablas | Solo Attachment Counter | Todo lo demás |
| `cell_history_*` tablas | Solo Cell History | Todo lo demás |
| `dynamic_ddl_*` tablas | Solo Dynamic DDL | Todo lo demás |
| `batr_*` tablas | Solo Bulk Uploader | Todo lo demás |
| `textnow_configurations` | Solo TextNow | Todo lo demás |
| Puerto de un backend | Frontend + ningún backend | Backends entre sí |
| `crypto_helper.py` en un backend | Solo ese backend | Los demás (copias independientes) |
| Env var de un solo backend | Solo ese backend | Todo lo demás |

---

## Código copy-paste: riesgo de divergencia silenciosa

`crypto_helper.py` existe como copia independiente en:

```
backend/attachment-counter/crypto_helper.py
backend/bulk-attachment-uploader/crypto_helper.py
backend/cell-history/crypto_helper.py
backend/dynamic-ddl/crypto_helper.py
backend/id-checker/crypto_helper.py
backend/textnow/crypto_helper.py
```

**Riesgo:** Un bug de seguridad arreglado en uno NO se propaga a los demás.
**Solución planeada:** Migrar a `smarthub_core.crypto` (Fase 0 del plan de migración).
**Mientras tanto:** Si corriges un bug en `crypto_helper.py`, corregir en los 6 archivos.

---

## Dependencias del frontend

El frontend llama directamente a cada backend. Si cambia la URL o el puerto de un backend, el frontend falla para esa herramienta.

```
REACT_APP_ENCRYPT_URL        → encrypt-tool (3051)
REACT_APP_CH_BACKEND_URL     → cell-history (3014)
REACT_APP_ID_CHECKER_BACKEND_URL → id-checker (3002)
REACT_APP_BAU_BACKEND_URL    → bulk-attachment-uploader (3003)
REACT_APP_AC_API_BACKEND_URL → attachment-counter (3005)
REACT_APP_DDL_BACKEND_URL    → dynamic-ddl (3007)
REACT_APP_I2C_BACKEND_URL    → image-to-cell-3 (3011)
```

---

## Protocolo antes de hacer un cambio

```
¿El cambio toca APP_CRYPTO_KEY, smartsheet_tokens, user_profiles,
 organization_subscriptions, o organization_ss_api_base?

  SÍ → Coordinar con todos los backends
       → Preparar rollback antes de ejecutar
       → Deployar todos los backends en la misma ventana de mantenimiento
       → Ver CONSTRAINTS.md para el protocolo detallado

  NO, pero toca una tabla compartida por 2+ backends →
       → Revisar tabla de impacto arriba
       → Crear migración de schema con backward compatibility
       → Deployar en orden: migración DB → backends afectados

  NO, solo afecta un backend →
       → Puedes modificar de forma independiente
       → Pull request normal al branch del backend
```
