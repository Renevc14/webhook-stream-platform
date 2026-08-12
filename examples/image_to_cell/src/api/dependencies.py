"""
API Dependencies
================

FastAPI dependency injection providers.
"""

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from src.config.settings import Settings, get_settings
from src.core.processor import ImageProcessor
from src.workers.webhook_handler import WebhookHandler, get_webhook_handler
from src.workers.backfill import BackfillWorker, get_backfill_worker
from src.services.supabase.client import SupabaseService, get_supabase_service


log = logging.getLogger(__name__)


# Settings dependency
def get_app_settings() -> Settings:
    """Get application settings."""
    return get_settings()


# Processor dependency
_processor: Optional[ImageProcessor] = None


def get_processor(
    settings: Settings = Depends(get_app_settings),
) -> ImageProcessor:
    """Get the image processor singleton."""
    global _processor
    if _processor is None:
        _processor = ImageProcessor(settings=settings)
    return _processor


# Worker dependencies
def get_webhook_handler_dep() -> WebhookHandler:
    """Get webhook handler."""
    return get_webhook_handler()


def get_backfill_worker_dep() -> BackfillWorker:
    """Get backfill worker."""
    return get_backfill_worker()


# Database dependency
def get_db() -> SupabaseService:
    """Get database service."""
    return get_supabase_service()


# Header extraction
async def get_webhook_signature(
    smartsheet_hook_response: Optional[str] = Header(None, alias="Smartsheet-Hook-Response"),
    smartsheet_hmac_sha256: Optional[str] = Header(None, alias="Smartsheet-Hmac-SHA256"),
) -> dict:
    """Extract webhook-related headers."""
    return {
        "challenge": smartsheet_hook_response,
        "signature": smartsheet_hmac_sha256,
    }


async def get_organization_id(
    x_organization_id: Optional[str] = Header(None, alias="X-Organization-ID"),
) -> Optional[str]:
    """Extract organization ID from header."""
    return x_organization_id


async def require_organization_id(
    org_id: Optional[str] = Depends(get_organization_id),
) -> str:
    """Require organization ID in header."""
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-ID header is required",
        )
    return org_id


async def get_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> Optional[str]:
    """Extract user ID from header."""
    return x_user_id


# Admin key for /dlq, /metrics (pen-test ready, BAU-aligned)
async def require_admin_key(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    settings: Settings = Depends(get_app_settings),
) -> str:
    """Require X-Admin-Key header matching I2C_ADMIN_API_KEY for admin endpoints."""
    if not settings.i2c_admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints are not configured. Set I2C_ADMIN_API_KEY.",
        )
    if not x_admin_key or x_admin_key != settings.i2c_admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key",
        )
    return x_admin_key


# API Key validation (for internal services)
async def validate_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    settings: Settings = Depends(get_app_settings),
) -> bool:
    """Validate API key for internal service calls."""
    if not settings.api_key:
        return True  # No key configured, allow all

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return True
