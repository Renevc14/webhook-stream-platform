# Coding Standards — Smarthub

## Core Principles

1. **English only** — all code, comments, variable names, log messages, and docstrings must be written in English.
2. **Async by default** — FastAPI is async. Every function that can be `async def` must be.
3. **Fail fast, fail loudly** — explicit errors with full context. Never silently swallow exceptions.
4. **State in Redis, not in process** — nothing important lives only in process memory.
5. **Always retry Supabase and Smartsheet** — every external call must use retry logic with exponential backoff.
6. **Every feature must be testable** — no hidden state, no hardcoded singletons, inject dependencies.
7. **Smartsheet IDs are BIGINT** — never assume they fit in 32 bits.
8. **Log every important event and every error** — if something goes wrong in production, the logs must explain why.

---

## Language

**All code must be written in English.** This includes:
- Variable and function names
- Class names and module names
- Comments and docstrings
- Log messages
- Exception messages
- Commit messages
- SQL column/table names

```python
# WRONG
def procesar_imagen(ruta_archivo: str) -> bytes:
    # convierte la imagen al formato correcto
    ...

# CORRECT
def process_image(file_path: str) -> bytes:
    # convert image to the required format
    ...
```

---

## Retry Policy

### Rule: every Supabase call must use execute_with_retry()

```python
# WRONG — no retry, fails permanently on transient network error
result = db.table("t").select("*").execute()  # ❌

# CORRECT — automatic retry with exponential backoff
result = db.execute_with_retry(
    lambda: db.table("t").select("*").eq("org_id", org_id).execute(),
    operation_description="fetch active configs for org"  # shown in logs on retry
)
```

### Rule: every Smartsheet API call must handle 429 and 5xx with retry

```python
import asyncio
import random
import logging

log = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BASE_DELAY_S = 1.0
MAX_DELAY_S = 60.0


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter to avoid thundering herd."""
    delay = min(BASE_DELAY_S * (2 ** attempt), MAX_DELAY_S)
    return delay * (0.5 + random.random() * 0.5)


async def call_smartsheet_with_retry(fn, operation: str):
    """
    Execute a Smartsheet SDK call with retry on transient errors.

    Args:
        fn: Callable (sync) that performs the Smartsheet operation.
        operation: Human-readable description used in log messages.

    Raises:
        SmartsheetException: On fatal error (4xx except 429) or exhausted retries.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = fn()
            if attempt > 0:
                log.info(f"[SMARTSHEET:RETRY] {operation} succeeded on attempt {attempt + 1}")
            return result

        except Exception as e:
            status_code = getattr(e, "error_code", None) or getattr(e, "status_code", None)
            is_retryable = status_code in RETRYABLE_STATUS_CODES

            if not is_retryable or attempt == MAX_ATTEMPTS - 1:
                log.error(
                    f"[SMARTSHEET:ERROR] {operation} failed after {attempt + 1} attempts: "
                    f"status={status_code} error={e}"
                )
                raise

            delay = _backoff_delay(attempt)
            log.warning(
                f"[SMARTSHEET:RETRY] {operation} failed (attempt {attempt + 1}/{MAX_ATTEMPTS}), "
                f"status={status_code}, retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
```

### Retry classification

| Error | Type | Action |
|---|---|---|
| Smartsheet 429 | Retryable | Backoff + retry |
| Smartsheet 500/502/503/504 | Retryable | Backoff + retry |
| Smartsheet 401/403 | Fatal | Log + send to DLQ |
| Smartsheet 404 | Fatal | Log + skip |
| Supabase connection timeout | Retryable | execute_with_retry() |
| Supabase 0 rows | Not an error | Use `.limit(1)` and check `result.data` |
| Corrupt/unreadable image | Fatal | Log + send to DLQ |
| Redis connection error | Retryable | Backoff + retry |

---

## Logging

### Rule: log every significant event and every error path

Every operation that can fail in production must have logs that allow diagnosis without needing to reproduce the issue.

**Required log points:**
- Start of processing a job/event (INFO)
- Successful completion with duration (INFO)
- Retryable error with attempt count (WARNING)
- Fatal error before sending to DLQ (ERROR)
- Any external API call that fails (WARNING or ERROR)
- Configuration state changes (INFO)
- Webhook registration / deregistration (INFO)
- Rate limit hits (WARNING)

```python
import logging
import time

log = logging.getLogger(__name__)

# Prefix format: [TOOL:OPERATION] for easy grep in production logs
# Always include: org_id, relevant IDs, duration on completion

async def process_row(config_id: str, row_id: int, org_id: str) -> None:
    log.info(f"[I2C:PROCESS] Starting row={row_id} config={config_id} org={org_id}")
    start = time.monotonic()

    try:
        result = await _do_processing(config_id, row_id)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.info(
            f"[I2C:PROCESS] Completed row={row_id} org={org_id} "
            f"attachments={result.count} duration_ms={elapsed_ms}"
        )

    except RetryableError as e:
        log.warning(f"[I2C:PROCESS] Retryable error row={row_id} org={org_id}: {e}")
        raise

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.error(
            f"[I2C:PROCESS] Fatal error row={row_id} org={org_id} "
            f"duration_ms={elapsed_ms}: {e}",
            exc_info=True  # always include stack trace on ERROR
        )
        raise FatalError(str(e)) from e
```

### Log levels

| Level | Use for |
|---|---|
| `DEBUG` | Internal state values, useful for local debugging only. Not for production. |
| `INFO` | Normal system events: webhook received, job completed, config created. |
| `WARNING` | Abnormal but recoverable: retry triggered, webhook disabled and re-registering, rate limit hit. |
| `ERROR` | Failure requiring attention: job sent to DLQ, unrecoverable API error, startup failure. |

### Log prefix convention

```
[TOOL:OPERATION]

Examples:
[I2C:WEBHOOK]      incoming webhook events
[I2C:PROCESS]      image processing
[I2C:LIFECYCLE]    webhook registration and health
[I2C:DLQ]          dead letter queue operations
[GATEWAY:HMAC]     signature validation
[GATEWAY:ENQUEUE]  job enqueue to Redis stream
[CELL-H:POLL]      cell history polling
[SUPABASE:RETRY]   database retry attempts
[SMARTSHEET:RETRY] API retry attempts
```

---

## Testability

### Rule: every feature must be independently testable

Design for testability from the start:
- **Inject dependencies** — never instantiate external clients inside business logic functions.
- **No module-level singletons** that can't be overridden in tests.
- **Pure functions** where possible — same input, same output, no side effects.
- **Separate I/O from logic** — fetching data and processing data should be different functions.

```python
# WRONG — impossible to test without hitting real Supabase
async def get_active_configs():
    db = SupabaseService()  # ❌ hardcoded instantiation
    return db.table("configs").select("*").execute()

# CORRECT — injectable, testable
async def get_active_configs(db: SupabaseService) -> list[dict]:
    result = db.execute_with_retry(
        lambda: db.table("configs").select("*").eq("paused", False).execute(),
        "get active configs"
    )
    return result.data or []
```

### Test structure

```
backend/{tool}/
└── tests/
    ├── conftest.py           # shared fixtures (mock db, mock redis, mock smartsheet)
    ├── unit/
    │   ├── test_processor.py # pure logic, no I/O
    │   └── test_models.py    # pydantic validation
    └── integration/
        ├── test_routes.py    # FastAPI TestClient, mocked external services
        └── test_worker.py    # worker logic with mocked stream and DB
```

### Required fixtures in conftest.py

```python
# tests/conftest.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config.settings import Settings


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        supabase_url="https://test.supabase.co",
        supabase_service_role_key="test-key",
        redis_url="redis://localhost:6379",
        app_crypto_key="dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleQ==",  # 32-byte test key
        product_slug="test-tool",
    )


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.execute_with_retry = MagicMock(side_effect=lambda fn, _desc: fn())
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.set.return_value = True   # lock acquired by default
    redis.get.return_value = None   # cache miss by default
    return redis


@pytest.fixture
def mock_smartsheet():
    client = MagicMock()
    return client


@pytest.fixture
def client(test_settings, mock_db, mock_redis):
    app = create_app(settings=test_settings)
    app.dependency_overrides[get_db] = lambda: mock_db
    return TestClient(app)
```

### Unit test example

```python
# tests/unit/test_processor.py
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_process_row_success(mock_db, mock_smartsheet):
    """Processing a valid row returns success with attachment count."""
    mock_smartsheet.Sheets.get_row.return_value = MagicMock(
        attachments=[MagicMock(id=1), MagicMock(id=2)]
    )

    result = await process_row(
        config_id="config-123",
        row_id=5315099586416516,
        org_id="org-abc",
        db=mock_db,
        ss_client=mock_smartsheet,
    )

    assert result.success is True
    assert result.attachments_processed == 2


@pytest.mark.asyncio
async def test_process_row_skips_duplicate(mock_redis):
    """Duplicate row within dedup window is skipped without processing."""
    mock_redis.set.return_value = False  # lock already held

    result = await process_row_with_dedup(
        config_id="config-123",
        row_id=5315099586416516,
        org_id="org-abc",
        redis=mock_redis,
    )

    assert result.skipped is True
    assert result.reason == "duplicate"


@pytest.mark.asyncio
async def test_process_row_retries_on_429(mock_smartsheet):
    """Smartsheet 429 triggers retry with backoff, succeeds on second attempt."""
    call_count = 0

    def ss_get_row(*args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SmartsheetException(status_code=429)
        return MagicMock(attachments=[])

    mock_smartsheet.Sheets.get_row.side_effect = ss_get_row

    result = await process_row(...)
    assert result.success is True
    assert call_count == 2  # failed once, succeeded on retry


@pytest.mark.asyncio
async def test_process_row_sends_to_dlq_on_fatal_error(mock_db, mock_redis):
    """Unrecoverable error sends job to DLQ and does not raise."""
    mock_db.execute_with_retry.side_effect = Exception("config not found")

    with pytest.raises(FatalError):
        await process_row(config_id="nonexistent", ...)
```

### Integration test example

```python
# tests/integration/test_routes.py
import pytest
from fastapi.testclient import TestClient


def test_create_config_returns_201(client, mock_db):
    mock_db.table.return_value.insert.return_value.execute.return_value.data = [
        {"id": "new-id", "organization_id": "org-abc", "worksheet_id": 123}
    ]

    response = client.post(
        "/configs/",
        json={"worksheet_id": 123, "configuration_name": "Test"},
        headers={"X-Organization-Id": "org-abc"},
    )

    assert response.status_code == 201
    assert response.json()["id"] == "new-id"


def test_webhook_challenge_responds_immediately(client):
    """Challenge response must not be blocked by any dependency."""
    challenge_token = "test-challenge-12345"

    response = client.post(
        "/webhook/smartsheet",
        headers={"Smartsheet-Hook-Challenge": challenge_token},
        json={},
    )

    assert response.status_code == 200
    assert response.json()["smartsheetHookResponse"] == challenge_token
    assert response.headers["Smartsheet-Hook-Response"] == challenge_token


def test_webhook_rejects_invalid_hmac(client):
    response = client.post(
        "/webhook/smartsheet",
        headers={"Smartsheet-Hmac-SHA256": "invalid-signature"},
        json={"events": []},
    )
    assert response.status_code == 401
```

---

## Security

### Never log sensitive data

```python
# WRONG — leaks API token to logs
log.info(f"Using token: {api_token}")  # ❌

# CORRECT — log only safe metadata
log.info(f"Using token for org={org_id} (ends in ...{api_token[-4:]})")  # ✅
```

### Never return internal error details to the client

```python
# WRONG — exposes internal state and stack traces
raise HTTPException(status_code=500, detail=str(e))  # ❌

# CORRECT — generic message to client, full detail in logs
log.error(f"[ROUTE:CREATE] Unexpected error: {e}", exc_info=True)
raise HTTPException(status_code=500, detail="Internal server error")  # ✅
```

### Always validate org_id ownership before returning or modifying data

```python
# WRONG — any authenticated user can read any config
result = db.table("configs").select("*").eq("id", config_id).execute()  # ❌

# CORRECT — always scope to the requesting org
result = db.table("configs").select("*") \
    .eq("id", config_id) \
    .eq("organization_id", org_id) \  # ✅ org isolation
    .limit(1).execute()
```

### Validate HMAC on every webhook

```python
import hmac
import hashlib

def validate_smartsheet_hmac(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify Smartsheet webhook signature.
    Returns False if signature is missing, malformed, or invalid.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    # Use constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, signature)
```

### Input validation on all public endpoints

```python
from pydantic import BaseModel, Field, validator

class ConfigCreate(BaseModel):
    worksheet_id: int = Field(..., gt=0, description="Smartsheet worksheet ID (BIGINT)")
    configuration_name: str = Field(..., min_length=1, max_length=255)
    image_columns: list[int] = Field(..., min_items=1, max_items=10)

    @validator("configuration_name")
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("configuration_name cannot be blank")
        return v.strip()
```

---

## FastAPI patterns

### Route structure

```python
# backend/{tool}/src/api/routes/{resource}.py
import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import require_organization_id, get_user_id
from src.services.supabase.client import SupabaseService, get_db
from src.config.settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/{resources}", tags=["{resources}"])


@router.get("/{resource_id}", response_model=Dict[str, Any])
async def get_resource(
    resource_id: str,
    org_id: str = Depends(require_organization_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """Fetch a single resource by ID, scoped to the requesting organization."""
    result = db.execute_with_retry(
        lambda: db.table("{tool}_configurations")
            .select("*")
            .eq("id", resource_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute(),
        f"get resource {resource_id}"
    )
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    return result.data[0]
```

### Webhooks — challenge always first

```python
@router.post("/webhook/smartsheet")
async def webhook_handler(request: Request):
    # CRITICAL: challenge BEFORE any await or heavy Depends
    # Smartsheet has a ~30s timeout. FastAPI resolves all Depends before the handler.
    challenge = request.headers.get("Smartsheet-Hook-Challenge")
    if challenge:
        log.info("[GATEWAY:CHALLENGE] Responding to Smartsheet webhook challenge")
        return JSONResponse(
            content={"smartsheetHookResponse": challenge},
            headers={"Smartsheet-Hook-Response": challenge},
        )

    # Only here perform real work
    body = await request.json()
    await process_event(body)
    return {"status": "ok"}
```

### Use .limit(1) instead of .single()

```python
# WRONG — raises exception when 0 rows returned
result = db.table("t").select("*").eq("id", id).single().execute()  # ❌

# CORRECT — returns empty list, handle gracefully
result = db.table("t").select("*").eq("id", id).limit(1).execute()  # ✅
item = result.data[0] if result.data else None
```

---

## Async / Concurrency

### Never use time.sleep() in async code

```python
# WRONG — blocks the FastAPI event loop
async def register_webhook():
    time.sleep(2)  # ❌

# CORRECT
async def register_webhook():
    await asyncio.sleep(2)  # ✅
```

### Run blocking SDK calls in executor

```python
# Smartsheet SDK is synchronous. Run it in a thread pool to avoid blocking.
result = await asyncio.get_event_loop().run_in_executor(
    executor,
    lambda: smartsheet_client.Sheets.get_row(sheet_id, row_id)
)
```

### Parallel independent operations

```python
# WRONG — sequential when operations are independent
for org in orgs:
    await check_org_webhooks(org)  # ❌

# CORRECT — parallel with bounded concurrency
semaphore = asyncio.Semaphore(10)

async def bounded_check(org):
    async with semaphore:
        return await check_org_webhooks(org)

results = await asyncio.gather(
    *[bounded_check(org) for org in orgs],
    return_exceptions=True,  # one failure does not cancel the rest
)
```

---

## Redis

### Always set TTL on cache keys

```python
# WRONG — grows forever if cleanup fails
await redis.set(f"cache:{key}", value)  # ❌

# CORRECT
await redis.set(f"cache:{key}", value, ex=300)  # ✅ explicit TTL
```

### Deduplication with SET NX

```python
lock_key = f"dedup:i2c:{config_id}:{row_id}"
acquired = await redis.set(lock_key, "1", nx=True, ex=600)  # 10-min TTL
if not acquired:
    log.info(f"[I2C:DEDUP] Skipping duplicate: row={row_id} config={config_id}")
    return Result.skip(reason="duplicate")
```

### Redis key naming convention

```
{namespace}:{subtype}:{identifier}

stream:i2c                           Redis Stream for I2C jobs
stream:platform-events               Cross-tool event bus
cache:ss-token:{org_id}              Smartsheet token (TTL: 30min)
cache:workspaces:{org_id}            Workspace list (TTL: 10min)
cache:webhook-secret:{org}:{tool}    Webhook shared secret (TTL: 1h)
dedup:{tool}:{config_id}:{row_id}   Deduplication lock (TTL: 10min)
ratelimit:ss-api:{org_id}           Smartsheet rate limit counter
lock:lifecycle:master               Singleton lifecycle manager lock
```

---

## Smartsheet API

### Always include Change-Agent header on write operations

```python
# Without this header, Smartsheet fires a webhook back for every update,
# causing infinite loops with your own webhook handler.
CHANGE_AGENT = "smarthub-{tool_slug}"

smartsheet_client.Sheets.update_rows(
    sheet_id,
    rows,
    headers={"Smartsheet-Change-Agent": CHANGE_AGENT}  # ✅ required
)
```

### Always strip trailing Z from timestamps

```python
from dateutil.parser import isoparse

# Smartsheet sometimes returns: "2026-02-07T08:43:43+00:00Z"
# isoparse fails on the double timezone indicator
raw_timestamp = event.get("modifiedAt", "")
dt = isoparse(raw_timestamp.rstrip("Z"))  # ✅
```

### Smartsheet IDs: always BIGINT

```python
# In Pydantic models (Python int is 64-bit natively — OK)
class Config(BaseModel):
    worksheet_id: int
    row_id: int
    column_id: int

# In SQL (PostgreSQL INTEGER is 32-bit — overflow risk)
# WRONG:  worksheet_id INTEGER
# CORRECT: worksheet_id BIGINT
```

---

## Error classification

```python
class RetryableError(Exception):
    """Transient error — retry with exponential backoff."""

class FatalError(Exception):
    """Permanent error — send to DLQ, do not retry."""
```

| Scenario | Classification |
|---|---|
| Smartsheet 429 (rate limit) | `RetryableError` |
| Smartsheet 500/502/503/504 | `RetryableError` |
| Smartsheet 401/403 (auth) | `FatalError` |
| Smartsheet 404 (not found) | `FatalError` |
| Supabase connection timeout | `RetryableError` |
| Config not found in DB | `FatalError` |
| Corrupt/unreadable image | `FatalError` |
| Redis connection error | `RetryableError` |
| Invalid HMAC signature | `FatalError` |

---

## Pydantic models

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional
from datetime import datetime


class ProcessingMode(str, Enum):
    POLLING = "polling"
    WEBHOOK = "webhook"
    WEBHOOK_V3 = "webhook_v3"  # testing only


class ToolConfig(BaseModel):
    id: str
    organization_id: str
    worksheet_id: int        # BIGINT — Smartsheet uses 64-bit IDs
    processing_mode: ProcessingMode = ProcessingMode.WEBHOOK
    paused: bool = False
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}  # Pydantic v2
```

---

## Settings pattern

```python
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # Infrastructure (required in all backends)
    supabase_url: str
    supabase_service_role_key: str
    redis_url: str = Field(default="redis://localhost:6379")
    app_crypto_key: str  # shared across all backends — never rotate solo

    # Tool identity
    product_slug: str = Field(default="{tool-slug}")
    port: int = Field(default=3000)

    # Concurrency
    max_concurrent_jobs: int = Field(default=10, ge=1, le=50)

    # Smartsheet rate limit
    smartsheet_max_calls_per_minute: int = Field(default=275, ge=1, le=300)

    # Logging
    log_level: str = Field(default="INFO")

    model_config = {"env_file": ".env", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()
```
