"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

In production, systemd runs `uvicorn app.main:app --host 127.0.0.1 --port 8000`
and nginx TLS-terminates + reverse-proxies.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.config import get_settings
from app.routers import auto_enroll, enrollment, factory, health, telemetry
from app.services import influx

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Process-lifetime resources.

    Influx client is lazy-initialised on first write (see
    ``app.services.influx.get_client``), so startup has nothing to do.
    Shutdown must close it so in-flight writes get flushed and the HTTP
    session is released cleanly.
    """
    try:
        yield
    finally:
        await influx.close_client()
        log.info("shutdown: influx client closed")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.app_log_level.upper())

    app = FastAPI(
        title="Mikrotik CPE Cloud",
        version=__version__,
        summary="Central management + telemetry for the Bradford Broadband Mikrotik fleet.",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(factory.router)
    app.include_router(enrollment.router)
    app.include_router(auto_enroll.router)
    app.include_router(telemetry.router)

    return app


app = create_app()
