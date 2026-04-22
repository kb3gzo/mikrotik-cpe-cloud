"""Zero-touch auto-enrollment — called by the on-device script after first boot.

Stubbed in Phase 1.0 scaffold. Deliverable #5 in `01-design §12` fills this in
per `02-self-provisioning.md` §4.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/api/v1", tags=["auto-enroll"])


@router.post("/auto-enroll", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def auto_enroll() -> dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Auto-enrollment not yet implemented — see deliverable #5",
    )
