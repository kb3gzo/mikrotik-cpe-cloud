"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

In production, systemd runs `uvicorn app.main:app --host 127.0.0.1 --port 8000`
and nginx TLS-terminates + reverse-proxies.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app import __version__
from app.config import get_settings
from app.routers import auto_enroll, enrollment, factory, health, telemetry


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.app_log_level.upper())

    app = FastAPI(
        title="Mikrotik CPE Cloud",
        version=__version__,
        summary="Central management + telemetry for the Bradford Broadband Mikrotik fleet.",
    )

    app.include_router(health.router)
    app.include_router(factory.router)
    app.include_router(enrollment.router)
    app.include_router(auto_enroll.router)
    app.include_router(telemetry.router)

    return app


app = create_app()
