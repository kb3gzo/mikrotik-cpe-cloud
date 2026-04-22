"""Telemetry push endpoint — routers POST wireless stats here every 5 min.

Stubbed in Phase 1.0 scaffold. See `01-design-wireguard-and-telemetry.md` §5.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1", tags=["telemetry"])


@router.post("/telemetry", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def push_telemetry() -> dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Telemetry ingest not yet implemented — see design §5",
    )
