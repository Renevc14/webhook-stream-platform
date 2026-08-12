# Development Templates

Copy and adapt. Replace `{TOOL}` with the tool name in snake_case and `{Tool}` in PascalCase.

All code in templates is in English. Keep it that way.

---

## Template: Worker completo

```python
# workers/{tool}/src/worker/consumer.py
import asyncio
import logging
from typing import Any, Dict, Optional

from smarthub_core.worker.base import SmarthubWorker, Job, Result
from smarthub_core.db.redis import get_redis
from smarthub_core.db.supabase import get_supabase

log = logging.getLogger(__name__)


class {Tool}Worker(SmarthubWorker):
    tool_slug = "{tool}"          # debe coincidir con stream:slug en Redis
    stream_name = "stream:{tool}"

    async def process(self, job: Job) -> Result:
        """
        Procesa un job del stream. Llamado por la clase base.
        La deduplicación y el ACK son manejados por SmarthubWorker.
        """
        org_id = job.data["org_id"]
        worksheet_id = job.data["worksheet_id"]
        row_id = job.data["row_id"]
        config_id = job.data["config_id"]

        log.info(f"[{'{TOOL}'}:PROCESS] Starting row {row_id} for org {org_id}")
        start = asyncio.get_event_loop().time()

        try:
            # 1. Obtener configuración
            config = await self._get_config(config_id, org_id)
            if not config:
                return Result.skip(reason="config_not_found")

            if config.get("paused"):
                return Result.skip(reason="config_paused")

            # 2. Lógica principal de procesamiento
            result = await self._do_work(config, row_id, worksheet_id)

            # 3. Publicar evento al bus cross-tool
            await self.publish_event({
                "type": "{tool}_completed",
                "org_id": org_id,
                "worksheet_id": worksheet_id,
                "row_id": row_id,
                "config_id": config_id,
            })

            elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
            log.info(f"[{'{TOOL}'}:PROCESS] Completed row {row_id} in {elapsed}ms")
            return Result.success(data=result)

        except RetryableError as e:
            log.warning(f"[{'{TOOL}'}:PROCESS] Retryable error for row {row_id}: {e}")
            raise  # SmarthubWorker lo reencola con backoff

        except Exception as e:
            log.error(f"[{'{TOOL}'}:PROCESS] Fatal error for row {row_id}: {e}", exc_info=True)
            raise FatalError(str(e))  # SmarthubWorker lo manda a DLQ

    async def _get_config(self, config_id: str, org_id: str) -> Optional[Dict]:
        db = get_supabase()
        result = db.execute_with_retry(
            lambda: db.table("{tool}_configurations")
                .select("*")
                .eq("id", config_id)
                .eq("organization_id", org_id)
                .limit(1)
                .execute(),
            f"get config {config_id}"
        )
        return result.data[0] if result.data else None

    async def _do_work(self, config: Dict, row_id: int, worksheet_id: int) -> Dict:
        # Implementar lógica específica del tool aquí
        raise NotImplementedError


if __name__ == "__main__":
    worker = {Tool}Worker()
    asyncio.run(worker.run())
```

---

## Template: Ruta FastAPI con autenticación

```python
# src/api/routes/{recurso}.py
import logging
from typing import Optional, List, Dict, Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.api.dependencies import require_organization_id, get_user_id
from src.services.supabase.client import SupabaseService, get_db
from src.config.settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/{recursos}", tags=["{recursos}"])


# --- Modelos ---

class {Recurso}Create(BaseModel):
    name: str
    worksheet_id: int      # BIGINT — siempre int para IDs Smartsheet
    # ... campos específicos


class {Recurso}Update(BaseModel):
    name: Optional[str] = None
    paused: Optional[bool] = None


# --- Endpoints ---

@router.get("/", response_model=List[Dict[str, Any]])
async def list_{recursos}(
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> List[Dict[str, Any]]:
    result = db.execute_with_retry(
        lambda: db.table("{tool}_configurations")
            .select("*")
            .eq("organization_id", org_id)
            .execute(),
        "list {recursos}"
    )
    return result.data or []


@router.post("/", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_{recurso}(
    body: {Recurso}Create,
    org_id: str = Depends(require_organization_id),
    user_id: Optional[str] = Depends(get_user_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    resource_id = str(uuid.uuid4())
    data = {
        "id": resource_id,
        "organization_id": org_id,
        "created_by": user_id,
        **body.model_dump(),
    }
    result = db.execute_with_retry(
        lambda: db.table("{tool}_configurations").insert(data).execute(),
        f"create {recurso}"
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create")
    log.info(f"[{'{TOOL}'}:CONFIG] Created {resource_id} for org {org_id}")
    return result.data[0]


@router.patch("/{id}", response_model=Dict[str, Any])
async def update_{recurso}(
    id: str,
    body: {Recurso}Update,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = db.execute_with_retry(
        lambda: db.table("{tool}_configurations")
            .update(update_data)
            .eq("id", id)
            .eq("organization_id", org_id)
            .execute(),
        f"update {recurso} {id}"
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Not found")
    return result.data[0]


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_{recurso}(
    id: str,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> None:
    db.execute_with_retry(
        lambda: db.table("{tool}_configurations")
            .delete()
            .eq("id", id)
            .eq("organization_id", org_id)
            .execute(),
        f"delete {recurso} {id}"
    )
```

---

## Template: Settings de un backend

```python
# src/config/settings.py
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # --- Infraestructura (obligatorias) ---
    supabase_url: str
    supabase_service_role_key: str
    redis_url: str = Field(default="redis://localhost:6379")
    app_crypto_key: str  # compartida con todos los backends — no cambiar sola

    # --- Tool identity ---
    product_slug: str = Field(default="{tool-slug}")
    port: int = Field(default=3000)

    # --- Concurrencia ---
    max_concurrent_jobs: int = Field(default=10, ge=1, le=50)

    # --- Smartsheet ---
    smartsheet_max_calls_per_minute: int = Field(default=275, ge=1, le=300)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
```

---

## Template: Webhook handler (para gateway compartido)

```python
# services/webhook-gateway/src/routes/smartsheet.py
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from smarthub_core.smartsheet.webhook import validate_hmac
from smarthub_core.db.redis import get_redis, enqueue_job

log = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_TOOLS = {"i2c", "cell-history", "attachment-counter", "dynamic-ddl", "textnow"}


@router.post("/webhook/smartsheet/{tool_slug}/{org_id}")
async def handle_smartsheet_webhook(
    tool_slug: str,
    org_id: str,
    request: Request,
) -> JSONResponse:
    # 1. Challenge — SIEMPRE primero, sin awaits antes
    challenge = request.headers.get("Smartsheet-Hook-Challenge")
    if challenge:
        log.info(f"[GATEWAY:CHALLENGE] Tool={tool_slug} org={org_id}")
        return JSONResponse(
            content={"smartsheetHookResponse": challenge},
            headers={"Smartsheet-Hook-Response": challenge},
        )

    # 2. Validar tool
    if tool_slug not in SUPPORTED_TOOLS:
        return JSONResponse(status_code=404, content={"error": "unknown tool"})

    # 3. Validar HMAC
    body = await request.body()
    signature = request.headers.get("Smartsheet-Hmac-SHA256", "")
    redis = await get_redis()

    secret = await redis.get(f"cache:webhook-secret:{org_id}:{tool_slug}")
    if secret and not validate_hmac(body, signature, secret):
        log.warning(f"[GATEWAY:HMAC] Invalid signature for {tool_slug}/{org_id}")
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    # 4. Encolar — rápido, no bloquear
    payload = await request.json()
    await enqueue_job(
        stream=f"stream:{tool_slug}",
        data={
            "org_id": org_id,
            "tool": tool_slug,
            "payload": payload,
        }
    )

    log.info(f"[GATEWAY:ENQUEUE] Enqueued event for {tool_slug}/{org_id}")
    return JSONResponse(content={"status": "ok"})
```

---

## Template: Tabla SQL nueva (Supabase)

```sql
-- Crear tabla para un nuevo tool
-- Siempre incluir: id, organization_id, created_at, updated_at

CREATE TABLE {tool}_configurations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Identificadores Smartsheet (BIGINT obligatorio)
    worksheet_id BIGINT NOT NULL,

    -- Campos del tool
    configuration_name TEXT NOT NULL,
    paused BOOLEAN NOT NULL DEFAULT FALSE,

    -- Auditoría
    created_by UUID REFERENCES auth.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índices básicos
CREATE INDEX idx_{tool}_configs_org ON {tool}_configurations(organization_id);
CREATE INDEX idx_{tool}_configs_worksheet ON {tool}_configurations(worksheet_id);
CREATE INDEX idx_{tool}_configs_active ON {tool}_configurations(organization_id, paused)
    WHERE paused = FALSE;

-- RLS
ALTER TABLE {tool}_configurations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "{tool}_configs_org_isolation"
    ON {tool}_configurations
    FOR ALL
    USING (
        organization_id IN (
            SELECT organization_id FROM user_profiles WHERE user_id = auth.uid()
        )
    );

-- Auto-update de updated_at
CREATE TRIGGER update_{tool}_configs_updated_at
    BEFORE UPDATE ON {tool}_configurations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
```

---

## Template: .env.example para un backend

```bash
# Infraestructura (obligatorias — deben coincidir en TODOS los backends)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
APP_CRYPTO_KEY=your-32-byte-base64-key  # MUST match across all backends
REDIS_URL=redis://localhost:6379

# Tool identity
PRODUCT_SLUG={tool-slug}
PORT=3000

# Smartsheet
# Max 300 req/min global. Coordinado con otros backends via Redis (futuro).
SMARTSHEET_MAX_CALLS_PER_MINUTE=275

# Concurrencia
MAX_CONCURRENT_JOBS=10

# Logging
LOG_LEVEL=INFO  # DEBUG | INFO | WARNING | ERROR

# Development only
# APP_MODE=worker  # api | worker-{tool}
```
