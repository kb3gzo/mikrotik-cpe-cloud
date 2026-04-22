"""Liveness / readiness probes. Used by nginx and by monitoring.

Keep this module free of DB touches so a bad DB doesn't break the liveness
probe — we want the process reachable so we can debug it.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness — process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness — dependencies reachable. TODO: poke Postgres + Influx."""
    return {"status": "ok"}
