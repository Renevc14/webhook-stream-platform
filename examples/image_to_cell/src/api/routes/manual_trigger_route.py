"""
Manual trigger HTTP endpoint (hub "Run now").
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from src.services.i2c_bolt_gate import preflight_pause_if_blocked
from src.services.supabase.config_repo import ConfigRepository
from src.workers.manual_trigger import get_manual_trigger_worker
from src.models.results import ProcessingResult


router = APIRouter(tags=["Manual trigger"])


def _result_to_hub_payload(result: ProcessingResult) -> Dict[str, Any]:
    ok = bool(result.success) and not result.error
    dur_s = (result.processing_time_ms or 0) / 1000.0
    msg = (
        result.error
        if result.error
        else ("Configuration processed successfully" if ok else "Processing failed")
    )
    return {
        "success": ok,
        "message": msg,
        "rows_processed": result.rows_processed,
        "images_converted": result.total_images_processed,
        "processing_duration_seconds": round(dur_s, 3),
        "error": result.error,
    }


@router.post("/manual-trigger")
async def manual_trigger(
    request: Request,
    config_id: str = Query(..., description="Configuration UUID"),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> Any:
    """
    Run processing once for a configuration (same contract as legacy I2C v2 hub).
    """
    if not config_id or not config_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="config_id required")

    user_id = getattr(request.state, "i2c_jwt_sub", None) or x_user_id

    repo = ConfigRepository()
    config = repo.get_by_id(config_id.strip())
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuration not found")

    block = preflight_pause_if_blocked(config.organization_id, config.id)
    if block:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "success": False,
                "error": f"Processing blocked ({block})",
                "message": f"Processing blocked ({block})",
            },
        )

    worker = get_manual_trigger_worker()
    result = await worker.trigger(config_id.strip(), user_id)

    payload = _result_to_hub_payload(result)
    if not payload["success"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=payload.get("error") or payload.get("message") or "Processing failed",
        )
    return payload
