"""Smartsheet workspace/sheet picker with Redis cache.

Backs the admin UI's "pick a workspace / pick a sheet" flow without hammering
the Smartsheet API on every render. The list is read-mostly: most users
return to the picker after a few minutes/hours, so a 5-minute cache is the
right default. A ``force_refresh=True`` arg surfaces a "refresh" button in
the UI for users who just created a new workspace and need it immediately.

## Cache key

Per ``TARGET_ARCHITECTURE.md`` §Caching strategy, key 4:

- Key:   ``workspaces:{org_id}:{product_slug}``
- Shape: JSON list of ``WorkspaceSummary`` (without sheets — sheets are fetched
         lazily per workspace)
- TTL:   300 seconds (read-mostly; admin UI shows a refresh button)
- SoT:   Smartsheet API (cache is just to absorb repeat reads)
- Fallback on Redis-down: direct call to Smartsheet API

The picker only stores the **summary** of workspaces (id + name + permalink).
Sheets are listed under a different key so a workspace with hundreds of sheets
doesn't bloat the workspace cache.

## Authentication

The picker is constructed with a configured ``SmartsheetClient`` — the client
is the one that knows about the org's token + base URL. The picker itself is
provider-specific but stateless beyond what the client carries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import List, Optional

from smarthub_core.db.redis import RedisClient
from smarthub_core.platforms.smartsheet.client import (
    SmartsheetClient,
    SmartsheetError,
)

log = logging.getLogger(__name__)


_DEFAULT_TTL_S = 300


@dataclass(frozen=True)
class WorkspaceSummary:
    """Minimal info about a Smartsheet workspace.

    The picker returns this — not the full workspace tree — because admin UI
    only needs id+name to render the first level. Drilling into a workspace
    triggers a separate ``list_workspace_contents`` call.
    """

    id: int
    name: str
    permalink: Optional[str] = None


@dataclass(frozen=True)
class SheetSummary:
    """Minimal info about a Smartsheet sheet."""

    id: int
    name: str
    permalink: Optional[str] = None
    workspace_id: Optional[int] = None


class SmartsheetPicker:
    """Workspace/sheet picker bound to a single ``(org_id, product_slug)``.

    Usage:
        picker = SmartsheetPicker(client=ss_client, redis=rc)
        workspaces = await picker.list_workspaces()         # cached 5 min
        sheets = await picker.list_workspace_sheets(ws_id)  # not cached
        await picker.invalidate_workspaces()                # after a new one
    """

    def __init__(
        self,
        *,
        client: SmartsheetClient,
        redis: RedisClient,
        cache_ttl_s: int = _DEFAULT_TTL_S,
    ) -> None:
        self._client = client
        self._redis = redis
        self._cache_ttl_s = cache_ttl_s
        self._workspaces_key = (
            f"workspaces:{client.org_id}:{client.product_slug}"
        )

    @property
    def cache_key_workspaces(self) -> str:
        return self._workspaces_key

    # ----- workspaces -----------------------------------------------------

    async def list_workspaces(
        self,
        *,
        force_refresh: bool = False,
    ) -> List[WorkspaceSummary]:
        """Return all workspaces visible to this org's PAT.

        Cached for ``cache_ttl_s`` seconds. Pass ``force_refresh=True`` to
        bypass the cache (admin UI's "refresh" button).
        """
        if not force_refresh:
            cached = await self._redis.cache_get(self._workspaces_key)
            if cached is not None:
                try:
                    items = json.loads(cached)
                    return [WorkspaceSummary(**i) for i in items]
                except (json.JSONDecodeError, TypeError) as e:
                    log.warning(
                        "smartsheet_picker: corrupt workspaces cache for org=%s slug=%s — re-fetching: %s",
                        self._client.org_id,
                        self._client.product_slug,
                        e,
                    )
                    await self._redis.cache_del(self._workspaces_key)

        result = await self._client.call_with_retry_async(
            lambda c: c.Workspaces.list_workspaces(include_all=True),
            description=f"list_workspaces org={self._client.org_id}",
        )

        items = [
            WorkspaceSummary(
                id=int(ws.id),
                name=ws.name,
                permalink=getattr(ws, "permalink", None),
            )
            for ws in (result.data or [])
        ]

        await self._redis.cache_set(
            self._workspaces_key,
            json.dumps([asdict(i) for i in items]),
            ttl_s=self._cache_ttl_s,
        )
        return items

    async def invalidate_workspaces(self) -> None:
        """Drop the cached workspaces list. Call after creating/deleting a
        workspace."""
        await self._redis.cache_del(self._workspaces_key)

    # ----- sheets within a workspace --------------------------------------

    async def list_workspace_sheets(
        self,
        workspace_id: int,
    ) -> List[SheetSummary]:
        """List sheets directly inside ``workspace_id`` (not recursive into
        folders). Not cached — the picker assumes this is called on user
        click and a stale list is more confusing than a fresh API call.
        """
        result = await self._client.call_with_retry_async(
            lambda c: c.Workspaces.get_workspace(workspace_id, load_all=False),
            description=f"get_workspace ws={workspace_id}",
        )
        ws = result if hasattr(result, "sheets") else None
        if ws is None:
            raise SmartsheetError(
                f"unexpected get_workspace response for ws={workspace_id}"
            )
        sheets = getattr(ws, "sheets", None) or []
        return [
            SheetSummary(
                id=int(s.id),
                name=s.name,
                permalink=getattr(s, "permalink", None),
                workspace_id=workspace_id,
            )
            for s in sheets
        ]

    # ----- sheet columns --------------------------------------------------

    async def list_sheet_columns(self, sheet_id: int) -> list:
        """Return the column list of ``sheet_id`` as raw SDK objects.

        Not cached — column lists change rarely but configuration UIs need
        to reflect column adds/removes immediately. Caller can transform
        the SDK objects however it needs.
        """
        result = await self._client.call_with_retry_async(
            lambda c: c.Sheets.get_columns(sheet_id, include_all=True),
            description=f"get_columns sheet={sheet_id}",
        )
        return list(result.data or [])
