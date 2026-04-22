"""Factory pre-provisioning installer endpoint.

Stubbed in Phase 1.0 scaffold. Deliverable #4 in `01-design §12` fills this in
per `02-self-provisioning.md` §3.2.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/factory", tags=["factory"])


@router.get("/self-enroll.rsc", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def factory_installer() -> dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Factory installer not yet implemented — see deliverable #4",
    )
