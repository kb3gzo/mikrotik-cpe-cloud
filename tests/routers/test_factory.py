"""Integration tests for `GET /factory/self-enroll.rsc`.

We stand up a FastAPI TestClient with the DB dependency swapped for an async
SQLite session. The provisioning secret is injected via get_settings override.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.models import AdminFetchToken
from app.models.base import Base
from app.routers import factory
from app.services.tokens import issue_admin_fetch_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sm():
    """Sessionmaker bound to an in-memory SQLite DB with the AdminFetchToken
    table created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: AdminFetchToken.__table__.create(sc)
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_token(sm):
    """Mint a valid fetch token and return (raw, row_id)."""
    async with sm() as s:
        result = await issue_admin_fetch_token(
            s, label="test-bench", ttl_hours=1, issued_by="pytest"
        )
        await s.commit()
        return result.raw, result.row.id


@pytest.fixture
def app(sm) -> FastAPI:
    """A minimal FastAPI app mounting only the factory router, with
    get_session + get_settings overridden."""
    app = FastAPI()
    app.include_router(factory.router)

    async def override_get_session():
        async with sm() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    def override_get_settings() -> Settings:
        return Settings(
            provisioning_secret_current="TEST_SECRET_ABCDEF",
            server_fqdn="test.example.com",
            enrollment_url="https://test.example.com/api/v1/auto-enroll",
        )

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = override_get_settings
    # The factory module calls get_settings() directly (not via Depends), so
    # we also monkeypatch the module-level reference.
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_missing_token_is_422(client):
    r = client.get("/factory/self-enroll.rsc")
    # FastAPI returns 422 for missing required query params
    assert r.status_code == 422


def test_invalid_token_returns_401_rsc_error(client):
    r = client.get("/factory/self-enroll.rsc?t=thisisnotarealtokenvalue123")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/plain")
    assert ":log error" in r.text
    assert "fetch token" in r.text


def test_valid_token_returns_200_with_rendered_installer(
    client, seeded_token, monkeypatch
):
    raw, _ = seeded_token
    # Patch get_settings inside the factory module so the handler picks up
    # the provisioning secret we set in the app fixture.
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(
            provisioning_secret_current="TEST_SECRET_ABCDEF",
            server_fqdn="test.example.com",
            enrollment_url="https://test.example.com/api/v1/auto-enroll",
        ),
    )
    r = client.get(f"/factory/self-enroll.rsc?t={raw}")
    assert r.status_code == 200
    body = r.text
    # The template expanded with our values
    assert "TEST_SECRET_ABCDEF" in body
    assert "test.example.com" in body
    assert "https://test.example.com/api/v1/auto-enroll" in body
    # Literal RouterOS code is present (sanity check the right template rendered)
    assert "/system script add name=cpe-cloud-enroll" in body
    assert "/system scheduler add name=cpe-cloud-enroll" in body
    # Operator-facing header comment
    assert 'fetch-token "test-bench"' in body


def test_missing_provisioning_secret_returns_500(client, seeded_token, monkeypatch):
    raw, _ = seeded_token
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(provisioning_secret_current=""),
    )
    r = client.get(f"/factory/self-enroll.rsc?t={raw}")
    assert r.status_code == 500
    assert "no active provisioning secret" in r.text
