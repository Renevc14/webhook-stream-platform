"""
Smartsheet Discovery Routes
===========================

Endpoints for listing workspaces, worksheets, and columns from Smartsheet.
Used by frontend for config creation.

Based on v2 implementation using direct REST API calls via httpx.
"""

import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, Tuple
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_user_id, get_db
from src.services.supabase.client import SupabaseService
from src.services.smartsheet.client import get_org_for_user, get_token_and_base
from src.config.constants import TABLE_NAMES


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/i2c", tags=["Smartsheet Discovery"])

# ─── Response Cache ───────────────────────────────────────────────────────────

_cache: Dict[str, Tuple[Any, float]] = {}
_WORKSPACES_TTL = 300   # 5 minutes
_WORKSHEETS_TTL = 300   # 5 minutes
_CACHE_MAX_SIZE = 200


def _get_cached(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and time.time() - entry[1] < entry[2]:
        return entry[0]
    return None


def _set_cached(key: str, data: Any, ttl: float) -> None:
    # Evict oldest entry when at capacity
    if len(_cache) >= _CACHE_MAX_SIZE and key not in _cache:
        oldest_key = min(_cache, key=lambda k: _cache[k][1])
        del _cache[oldest_key]
    _cache[key] = (data, time.time(), ttl)


# ─── Constants ───────────────────────────────────────────────────────────────

# Column types that support images (freetext columns)
FREETEXT_COLUMN_TYPES = {
    "TEXT_NUMBER",
    "TEXT",
    "MULTI_LINE_TEXT",
    "RICH_TEXT",
}

# Fullwidth slash to replace "/" in names (prevents path parsing issues)
FULLWIDTH_SLASH = "／"  # U+FF0F


# ─── Helpers ─────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    """
    Sanitize workspace/sheet/folder names.
    Replace literal '/' with fullwidth slash so path splitter isn't fooled.
    """
    if not name:
        return ""
    return name.replace("/", FULLWIDTH_SLASH).replace("<", "&lt;").replace(">", "&gt;")


@asynccontextmanager
async def smartsheet_client(token: str, base_url: str):
    """
    Create an authenticated httpx client for Smartsheet API.

    Args:
        token: Smartsheet API token
        base_url: API base URL (e.g., https://api.smartsheet.com/2.0)
    """
    # Ensure base_url ends with /2.0
    if not base_url.endswith("/2.0"):
        base_url = base_url.rstrip("/") + "/2.0"

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "smart-hub/i2c-v3",
        },
        timeout=30.0,
    ) as client:
        yield client


async def get_all_paginated(
    client: httpx.AsyncClient,
    endpoint: str,
    params: Optional[Dict] = None,
    page_size: int = 10000,
) -> List[Dict]:
    """
    Fetch all items from a paginated Smartsheet endpoint.

    Args:
        client: Authenticated httpx client
        endpoint: API endpoint path (e.g., "/workspaces")
        params: Additional query parameters
        page_size: Items per page (default 10,000 - Smartsheet max)

    Returns:
        List of all items across all pages
    """
    all_items = []
    page = 1
    params = params or {}

    while True:
        paginated_params = {**params, "page": page, "pageSize": page_size}

        response = await client.get(endpoint, params=paginated_params)
        response.raise_for_status()
        data = response.json()

        # Extract items (handle different response structures)
        items = data.get("data", [])
        all_items.extend(items)

        # Check pagination
        total_pages = data.get("totalPages", 1)

        log.debug(
            f"[SMARTSHEET] Fetched page {page}/{total_pages} from {endpoint} "
            f"({len(items)} items)"
        )

        if page >= total_pages:
            break

        page += 1

    return all_items


async def walk_folder(
    client: httpx.AsyncClient,
    folder_id: int,
    path: str,
    output: List[Dict],
    include_descriptions: bool = True,
    sem: Optional[asyncio.Semaphore] = None,
) -> None:
    """
    Recursively collect folders and sheets inside a folder.

    Args:
        client: Authenticated httpx client
        folder_id: Folder ID to walk
        path: Current path (e.g., "Parent/Child")
        output: Shared list to append results
        include_descriptions: Whether to include description field
        sem: Optional semaphore to limit concurrent Smartsheet API calls
    """
    # Fetch folder contents. `includeAll` is deprecated on the Smartsheet REST
    # API (errorCode 1018) — dropped here for forward-compat. `pageSize=10000`
    # is the documented Smartsheet max page size and preserves the original
    # "return everything" intent for any folder with ≤10k children.
    folder_params = {"pageSize": 10000}
    if sem:
        async with sem:
            response = await client.get(f"/folders/{folder_id}", params=folder_params)
    else:
        response = await client.get(f"/folders/{folder_id}", params=folder_params)
    response.raise_for_status()
    data = response.json()

    folder_name = sanitize(data.get("name", ""))
    full_path = f"{path}/{folder_name}" if path else folder_name

    # Add folder to output
    folder_item = {
        "id": folder_id,
        "name": full_path,
        "kind": "folder",
    }
    if include_descriptions:
        folder_item["description"] = f"Folder: {full_path}"
    output.append(folder_item)

    # Add sheets in this folder
    sheets = data.get("sheets", [])
    for sheet in sheets:
        sheet_name = sanitize(sheet.get("name", ""))
        sheet_item = {
            "id": sheet.get("id"),
            "name": f"{full_path}/{sheet_name}",
            "kind": "sheet",
        }
        if include_descriptions:
            sheet_item["description"] = f"Worksheet: {full_path}/{sheet_name}"
        output.append(sheet_item)

    log.debug(
        f"[SMARTSHEET] Folder {full_path}: {len(sheets)} sheets, "
        f"{len(data.get('folders', []))} subfolders"
    )

    # Recursively walk subfolders in parallel
    subfolders = data.get("folders", [])
    if subfolders:
        await asyncio.gather(*[
            walk_folder(client, sub["id"], full_path, output, include_descriptions, sem)
            for sub in subfolders
        ])


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/workspaces")
async def list_workspaces(
    user_id: str = Depends(get_user_id),
    db: SupabaseService = Depends(get_db),
) -> Dict[str, Any]:
    """
    List all Smartsheet workspaces for the user's organization.

    Returns:
        List of workspaces with id, name, description
    """
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id header required",
        )

    try:
        # Get organization for user
        org_id = get_org_for_user(user_id)

        cache_key = f"workspaces:{org_id}:{user_id}"
        cached = _get_cached(cache_key)
        if cached:
            log.info(f"[SMARTSHEET] Listed workspaces for user {user_id[:8]} (cached)")
            return cached

        # Get token and base URL
        token, base_url = get_token_and_base(org_id, user_id)

        async with smartsheet_client(token, base_url) as client:
            # Fetch all workspaces with pagination
            all_workspaces = await get_all_paginated(client, "/workspaces")

            workspaces = [
                {
                    "id": w.get("id"),
                    "name": sanitize(w.get("name", "")),
                    "description": f"Workspace for {sanitize(w.get('name', ''))}",
                }
                for w in all_workspaces
            ]

            log.info(f"[SMARTSHEET] Listed {len(workspaces)} workspaces for user {user_id[:8]}")

            result = {"workspaces": workspaces}
            _set_cached(cache_key, result, _WORKSPACES_TTL)
            return result

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[SMARTSHEET] Failed to list workspaces: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list workspaces: {str(e)}",
        )


@router.get("/workspaces/{workspace_id}/worksheets")
async def list_worksheets(
    workspace_id: int,
    user_id: str = Depends(get_user_id),
) -> Dict[str, Any]:
    """
    List all sheets and folders in a workspace.

    Returns:
        Folders, sheets, and reports (empty) in the workspace
    """
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id header required",
        )

    try:
        # Get organization for user
        org_id = get_org_for_user(user_id)

        cache_key = f"worksheets:{org_id}:{user_id}:{workspace_id}"
        cached = _get_cached(cache_key)
        if cached:
            log.info(f"[SMARTSHEET] Listed worksheets for workspace {workspace_id} (cached)")
            return cached

        # Get token and base URL
        token, base_url = get_token_and_base(org_id, user_id)

        async with smartsheet_client(token, base_url) as client:
            # Fetch workspace root. `includeAll` is deprecated on the Smartsheet
            # REST API (errorCode 1018) — dropped here for forward-compat.
            # `pageSize=10000` is the documented Smartsheet max page size and
            # preserves the original "return everything" intent.
            response = await client.get(
                f"/workspaces/{workspace_id}",
                params={"pageSize": 10000},
            )
            response.raise_for_status()
            ws_data = response.json()

            # Process root-level sheets
            root_sheets = [
                {
                    "id": s.get("id"),
                    "name": sanitize(s.get("name", "")),
                    "kind": "sheet",
                    "description": f"Worksheet: {sanitize(s.get('name', ''))}",
                }
                for s in ws_data.get("sheets", [])
            ]

            # Process root-level folders
            root_folders = [
                {
                    "id": f.get("id"),
                    "name": sanitize(f.get("name", "")),
                    "kind": "folder",
                    "description": f"Folder: {sanitize(f.get('name', ''))}",
                }
                for f in ws_data.get("folders", [])
            ]

            log.info(
                f"[SMARTSHEET] Workspace {workspace_id}: "
                f"{len(root_sheets)} root sheets, {len(root_folders)} root folders"
            )

            # Recursively walk all folders in parallel (max 10 concurrent API calls)
            all_items: List[Dict] = []
            folder_ids = [f.get("id") for f in ws_data.get("folders", []) if f.get("id")]
            sem = asyncio.Semaphore(10)

            if folder_ids:
                await asyncio.gather(*[
                    walk_folder(client, fid, "", all_items, include_descriptions=True, sem=sem)
                    for fid in folder_ids
                ])

            # Separate folders and sheets from recursive walk
            sub_folders = [item for item in all_items if item.get("kind") == "folder"]
            sub_sheets = [item for item in all_items if item.get("kind") == "sheet"]

            # Combine root and sub items
            all_folders = root_folders + sub_folders
            all_sheets = root_sheets + sub_sheets

            # Sort alphabetically
            all_folders.sort(key=lambda x: x.get("name", "").lower())
            all_sheets.sort(key=lambda x: x.get("name", "").lower())

            log.info(
                f"[SMARTSHEET] Workspace {workspace_id} total: "
                f"{len(all_sheets)} sheets, {len(all_folders)} folders"
            )

            result = {
                "folders": all_folders,
                "sheets": all_sheets,
                "reports": [],  # Always empty for Image to Cell
            }
            _set_cached(cache_key, result, _WORKSHEETS_TTL)
            return result

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[SMARTSHEET] Failed to list worksheets: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list worksheets: {str(e)}",
        )


@router.get("/workspaces/{workspace_id}/worksheets/{worksheet_id}/columns")
async def list_columns(
    workspace_id: int,
    worksheet_id: int,
    user_id: str = Depends(get_user_id),
) -> Dict[str, Any]:
    """
    List columns in a worksheet that support images (freetext columns).

    Returns:
        List of freetext columns with metadata
    """
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-Id header required",
        )

    try:
        # Get organization for user
        org_id = get_org_for_user(user_id)

        # Get token and base URL
        token, base_url = get_token_and_base(org_id, user_id)

        async with smartsheet_client(token, base_url) as client:
            # Get ALL worksheet columns with pagination
            all_columns = await get_all_paginated(
                client,
                f"/sheets/{worksheet_id}/columns"
            )

            log.info(f"[SMARTSHEET] Sheet {worksheet_id}: Retrieved {len(all_columns)} total columns")

            # Filter to only freetext columns that can display images
            freetext_columns = []
            for col in all_columns:
                col_type = col.get("type")

                if col_type in FREETEXT_COLUMN_TYPES:
                    freetext_columns.append({
                        "id": col.get("id"),
                        "name": col.get("title"),
                        "title": col.get("title"),
                        "type": col_type,
                        "index": col.get("index"),
                        "systemColumnType": col.get("systemColumnType"),
                        "width": col.get("width"),
                        "primary": col.get("primary", False),
                        "locked": col.get("locked", False),
                        "lockedForUser": col.get("lockedForUser", False),
                    })

            log.info(
                f"[SMARTSHEET] Sheet {worksheet_id}: "
                f"{len(freetext_columns)} image-compatible columns "
                f"(filtered from {len(all_columns)} total)"
            )

            return {"columns": freetext_columns}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[SMARTSHEET] Failed to list columns: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list columns: {str(e)}",
        )
