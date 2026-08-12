"""
Health Check Routes
===================

Endpoints for health and readiness checks.
"""

import logging
import os
import psutil
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import APIRouter, Depends, Response, status

from src.api.dependencies import get_db, get_app_settings, require_admin_key
from src.config.settings import Settings
from src.config.constants import TABLE_NAMES
from src.services.supabase.client import SupabaseService
from src.workers.webhook_lifecycle import get_webhook_lifecycle_manager


log = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# Track service start time for uptime calculation
_start_time = time.time()


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """
    Basic health check.

    Returns 200 if the service is running.
    """
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "image-to-cell",
        "version": "3.0.0",
    }


@router.get("/ready")
async def readiness_check(
    response: Response,
    db: SupabaseService = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """
    Readiness check.

    Verifies that all dependencies are available.
    Returns 503 if any critical check fails.
    """
    checks = {}
    details = {}

    # Check 1: Database connection
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
                .select("id")
                .limit(1)
                .execute(),
            "health check"
        )
        checks["database"] = True
        details["database"] = "connected"
    except Exception as e:
        checks["database"] = False
        details["database"] = "connection failed"
        from src.utils.safe_log import safe_exception_message
        log.error(f"Database health check failed: {safe_exception_message(e)}")

    # Check 2: Memory usage
    try:
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        memory_mb = memory_info.rss / 1024 / 1024
        memory_percent = psutil.virtual_memory().percent

        checks["memory"] = memory_percent < settings.max_memory_usage_percent
        details["memory"] = {
            "used_mb": round(memory_mb, 2),
            "system_percent": round(memory_percent, 1),
            "threshold_percent": settings.max_memory_usage_percent,
        }
    except Exception as e:
        checks["memory"] = True  # Don't fail on memory check error
        details["memory"] = f"check failed: {e}"

    # Check 3: Webhook lifecycle manager
    try:
        lifecycle = get_webhook_lifecycle_manager()
        checks["webhook_lifecycle"] = lifecycle.is_running
        last_result = lifecycle.last_result
        details["webhook_lifecycle"] = {
            "running": lifecycle.is_running,
            "last_check": last_result.timestamp.isoformat() if last_result else None,
            "last_errors": last_result.errors if last_result else 0,
        }
    except Exception as e:
        checks["webhook_lifecycle"] = False
        details["webhook_lifecycle"] = str(e)

    # Check 4: Active configs count
    try:
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.CONFIG)
                .select("id", count="exact")
                .eq("paused", False)
                .execute(),
            "count active configs"
        )
        active_count = result.count if hasattr(result, 'count') else len(result.data or [])
        checks["configs"] = True
        details["configs"] = {"active_count": active_count}
    except Exception as e:
        checks["configs"] = False
        details["configs"] = str(e)

    # Check 5: Redis Lua scripts (Phase 1 — when REDIS_URL is configured)
    if settings.redis_url:
        try:
            from src.services.redis_client import get_redis_client

            redis_client = get_redis_client()
            if redis_client is None:
                checks["redis_lua"] = False
                details["redis_lua"] = "client not wired at startup"
            else:
                lua_ok = await redis_client.lua_scripts_health_check()
                checks["redis_lua"] = lua_ok
                details["redis_lua"] = (
                    "ok" if lua_ok else "lua scripts unavailable — restart task after Redis maintenance"
                )
        except Exception as e:
            checks["redis_lua"] = False
            details["redis_lua"] = str(e)
            log.warning("Redis Lua health check failed: %s", e)

    # Overall status - database is critical
    critical_checks = ["database"]
    all_critical_healthy = all(checks.get(c, False) for c in critical_checks)
    all_healthy = all(checks.values())

    if not all_critical_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if all_healthy else ("degraded" if all_critical_healthy else "not_ready"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - _start_time),
        "checks": checks,
        "details": details,
    }


@router.get("/info")
async def service_info(
    settings: Settings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """
    Service information.

    Returns configuration and version details.
    """
    return {
        "service": "image-to-cell",
        "version": "3.0.0",
        "environment": settings.environment,
        "uptime_seconds": int(time.time() - _start_time),
        "features": {
            "webhook_processing": True,
            "webhook_lifecycle": settings.webhook_lifecycle_enabled,
            "polling_processing": settings.enable_polling,
            "parallel_images": settings.enable_parallel_image_processing,
            "rate_limiting": settings.enable_rate_limiting,
            "backfill": True,
            "manual_trigger": True,
        },
        "limits": {
            "max_concurrent_orgs": settings.max_concurrent_orgs,
            "max_concurrent_rows": settings.max_concurrent_rows,
            "max_parallel_images": settings.max_parallel_images,
            "smartsheet_rate_limit": settings.smartsheet_max_calls_per_minute,
            "api_rate_limit": settings.api_rate_limit,
        },
        "webhook": {
            "callback_url": settings.webhook_callback_url,
            "gateway_callback_base_url": settings.smartsheet_webhook_callback_base_url,
            "lifecycle_interval_s": settings.webhook_lifecycle_interval_s,
        },
    }


@router.get("/metrics")
async def metrics(
    _admin: str = Depends(require_admin_key),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    Basic metrics endpoint.

    Returns processing statistics for monitoring.
    """
    metrics_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - _start_time),
    }

    # Get processing stats from database
    try:
        # Count today's processed images
        today = datetime.now(timezone.utc).date().isoformat()
        result = db.execute_with_retry(
            lambda: db.table(TABLE_NAMES.DAILY_USAGE)
                .select("images_processed, images_failed")
                .gte("usage_date", today)
                .execute(),
            "get daily metrics"
        )

        total_images = sum(r.get("images_processed", 0) for r in (result.data or []))
        total_errors = sum(r.get("images_failed", 0) for r in (result.data or []))

        metrics_data["today"] = {
            "images_processed": total_images,
            "images_failed": total_errors,
        }
    except Exception as e:
        metrics_data["today"] = {"error": str(e)}

    # Memory stats
    try:
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        metrics_data["memory"] = {
            "rss_mb": round(memory_info.rss / 1024 / 1024, 2),
            "vms_mb": round(memory_info.vms / 1024 / 1024, 2),
        }
        metrics_data["cpu_percent"] = process.cpu_percent()
    except Exception:
        pass

    # Webhook lifecycle stats
    try:
        lifecycle = get_webhook_lifecycle_manager()
        if lifecycle.last_result:
            metrics_data["webhook_health"] = lifecycle.last_result.to_dict()
    except Exception:
        pass

    return metrics_data
