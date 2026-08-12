"""Hub picker browse API — same shapes as id-checker (/workspaces,/items,/columns).

Browsing runs as the signed-in user through their own Smartsheet OAuth token, so
each user only ever sees the workspaces and sheets they actually have access to.
That is also why these routes need an organization: the token is per (org, user).

Smartsheet Python SDK 4.x removed Workspaces.get_workspace / Folders.get_folder —
use get_workspace_children / get_folder_children instead.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from smart_qr.adapters.core_infra import get_smartsheet_client
from smart_qr.adapters.smartsheet.credentials import (
    CredentialsMissing,
    CredentialsRevoked,
    CredentialsUnavailable,
    get_credential_resolver,
)
from smart_qr.api.deps import AuthContext, require_browse_auth
from smart_qr.config.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter(tags=["Browse"])


async def _core_for(auth: AuthContext):
    """A core SmartsheetClient bound to this user's credentials.

    Resolves first so missing / revoked / temporary map to the AUTH_ERROR
    shapes the Hub picker surfaces. The client itself then owns retries and
    the org-wide rate limit — the third home-grown retry loop this file used
    to carry is gone.
    """
    settings = get_settings()
    try:
        await get_credential_resolver(settings).resolve(
            auth.organization_id, auth.user_id
        )
    except (CredentialsMissing, CredentialsRevoked) as exc:
        raise HTTPException(status_code=401, detail=f"AUTH_ERROR: {exc}") from exc
    except CredentialsUnavailable as exc:
        log.error("Browse could not resolve credentials org=%s: %s", auth.organization_id, exc)
        raise HTTPException(status_code=503, detail=f"AUTH_ERROR: {exc}") from exc

    return await get_smartsheet_client(
        settings, auth.organization_id, auth.user_id
    )


def _is_folder(item: Any) -> bool:
    return "folder" in type(item).__name__.lower()


async def _list_children(
    core: Any, *, workspace_id: Optional[int], folder_id: Optional[int]
):
    """Paginate SDK v4 children endpoints into folders/sheets lists."""
    folders: list[dict[str, Any]] = []
    sheets: list[dict[str, Any]] = []
    last_key: Optional[str] = None
    while True:
        key = last_key

        def _page(client: Any, *, _key: Optional[str] = key) -> Any:
            if folder_id:
                return client.Folders.get_folder_children(
                    folder_id, last_key=_key, max_items=100
                )
            return client.Workspaces.get_workspace_children(
                workspace_id, last_key=_key, max_items=100
            )

        result = await core.call_with_retry_async(
            _page,
            description=(
                f"get_folder_children({folder_id})"
                if folder_id
                else f"get_workspace_children({workspace_id})"
            ),
        )
        for item in getattr(result, "data", None) or []:
            entry = {"id": int(item.id), "name": item.name or str(item.id)}
            if _is_folder(item):
                folders.append(entry)
            else:
                sheets.append(entry)
        last_key = getattr(result, "last_key", None) or None
        if not last_key:
            break
    return folders, sheets


@router.get("/workspaces")
async def list_workspaces(
    auth: AuthContext = Depends(require_browse_auth),
) -> list[dict[str, Any]]:
    core = await _core_for(auth)

    def _fetch(client: Any) -> list[dict[str, Any]]:
        try:
            result = client.Workspaces.list_workspaces(include_all=True)
        except TypeError:
            result = client.Workspaces.list_workspaces()
        rows = getattr(result, "data", None) or []
        out = []
        for ws in rows:
            wid = getattr(ws, "id", None)
            name = getattr(ws, "name", None)
            if wid is None:
                continue
            out.append({"id": int(wid), "name": name or str(wid)})
        log.info("Browse workspaces count=%s", len(out))
        return out

    try:
        return await core.call_with_retry_async(_fetch, description="list_workspaces")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("Browse workspaces failed: %s", exc)
        raise HTTPException(401, detail=f"AUTH_ERROR: {exc}") from exc


@router.get("/items")
async def list_items(
    workspace_id: Optional[int] = Query(None),
    folder_id: Optional[int] = Query(None),
    auth: AuthContext = Depends(require_browse_auth),
) -> dict[str, Any]:
    # Shared WorksheetPicker always sends workspace_id and adds folder_id when
    # drilling into folders. Prefer folder_id when both are present.
    if not workspace_id and not folder_id:
        raise HTTPException(400, detail="Provide workspace_id or folder_id")

    core = await _core_for(auth)

    try:
        folders, sheets = await _list_children(
            core,
            workspace_id=None if folder_id else workspace_id,
            folder_id=folder_id,
        )
        log.info(
            "Browse items workspace=%s folder=%s folders=%s sheets=%s",
            workspace_id,
            folder_id,
            len(folders),
            len(sheets),
        )
        return {"folders": folders, "sheets": sheets, "reports": []}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("Browse items failed: %s", exc)
        raise HTTPException(500, detail=str(exc)) from exc


@router.get("/columns")
async def list_columns(
    worksheet_id: Optional[int] = Query(None),
    sheet_id: Optional[int] = Query(None),
    auth: AuthContext = Depends(require_browse_auth),
) -> dict[str, Any]:
    sid = worksheet_id or sheet_id
    if not sid:
        raise HTTPException(400, detail="sheet_id or worksheet_id required")

    core = await _core_for(auth)

    def _fetch(client: Any) -> dict[str, Any]:
        sheet = client.Sheets.get_sheet(int(sid), page_size=1, page=1)
        out = []
        for col in sheet.columns or []:
            title = str(col.title) if col.title is not None else ""
            out.append(
                {
                    "id": str(int(col.id)) if col.id is not None else "0",
                    "title": title,
                    "name": title,
                    "type": None,
                }
            )
        log.info("Browse columns sheet=%s count=%s", sid, len(out))
        sheet_name = str(getattr(sheet, "name", None) or "").strip() or None
        return {
            "ok": True,
            "columns": out,
            "sheet_id": str(int(sid)),
            "sheet_name": sheet_name,
            "worksheet_name": sheet_name,
        }

    try:
        return await core.call_with_retry_async(
            _fetch, description=f"get_sheet_columns({sid})"
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("Browse columns failed: %s", exc)
        raise HTTPException(500, detail=str(exc)) from exc
