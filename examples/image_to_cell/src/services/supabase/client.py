"""
Supabase client — backed by smarthub_core.db.supabase.

This module is a thin shim over the shared library:

- ``SupabaseService`` and ``SupabaseError`` are re-exported from
  ``smarthub_core.db.supabase`` so existing imports keep working
  (``from src.services.supabase.client import SupabaseService``).
- ``get_supabase_service()`` builds and caches a singleton instance,
  preserving the prior dependency-injection pattern used across the
  service.

What the shared service does that the previous local implementation did not:

- Honours an **explicit httpx pool** (``SUPABASE_MAX_CONNECTIONS``,
  ``SUPABASE_MAX_KEEPALIVE``, ``SUPABASE_KEEPALIVE_EXPIRY_S``,
  ``SUPABASE_TIMEOUT_S``) instead of letting ``supabase-py`` default to
  ``max_connections=100`` which overruns Supabase compute-micro at scale.
- **Async retry uses ``asyncio.sleep``** so the event loop is not blocked
  during backoff (the previous async path called ``time.sleep`` and held
  the httpx connection).
- Retryable-error patterns include HTTP/2 ``ConnectionTerminated`` /
  ``RemoteProtocolError`` (the production GOAWAY frame previously surfaced
  as ``Failed to log image conversion: Server disconnected``).
- Bounded concurrency via ``SUPABASE_MAX_CONCURRENT_OPS`` (default 4).

Configure via environment variables. See ``.env.example`` for defaults.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from smarthub_core.db.supabase import SupabaseError, SupabaseService

from src.config.settings import Settings, get_settings


log = logging.getLogger(__name__)


__all__ = ["SupabaseError", "SupabaseService", "get_supabase_service"]


# Singleton instance
_supabase_service: Optional[SupabaseService] = None
_supabase_lock = Lock()


def get_supabase_service(settings: Optional[Settings] = None) -> SupabaseService:
    """Return the process-wide :class:`SupabaseService` singleton.

    The pool, timeouts, semaphore size, and retry behaviour are configured
    from ``smarthub_core.settings.CoreSettings`` (read from environment).
    """
    global _supabase_service

    with _supabase_lock:
        if _supabase_service is None:
            if settings is None:
                settings = get_settings()

            _supabase_service = SupabaseService(
                url=settings.supabase_url,
                key=settings.supabase_service_role_key,
            )
            log.info("Initialized Supabase service via smarthub_core.db.supabase")

        return _supabase_service
