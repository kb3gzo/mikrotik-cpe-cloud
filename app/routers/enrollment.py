"""Manual enrollment — retrofit and field-tech flow (§4.6 of design doc).

Stubbed in Phase 1.0 scaffold. Deliverable #6 in `01-design §12` fills this in.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1", tags=["enrollment"])


@router.post("/enroll", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def enroll() -> dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Manual enrollment not yet implemented — see deliverable #6",
    )
