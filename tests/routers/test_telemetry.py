"""Integration tests for POST /api/v1/telemetry (minimal Phase 1 path).

Covers: auth (missing/malformed/unknown/revoked bearer), router state gate
(active/pending OK, decommissioned/quarantined rejected), liveness update,
payload validation, and rate limiting. Influx writes live in a follow-up
task; this suite only exercises the handler's DB + response behaviour.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.models import Router, RouterToken
from app.models.base import Base
from app.routers import telemetry as telemetry_router
from app.services.rate_limit import _buckets
from app.services.tokens import hash_token, mint_telemetry_token, prefix_of


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sm():
    """In-memory SQLite sessionmaker. Same shape as test_auto_enroll."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc,
                tables=[Router.__table__, RouterToken.__table__],
                checkfirst=True,
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    _buckets.clear()
    yield
    _buckets.clear()


@pytest.fixture
def app(sm) -> FastAPI:
    app = FastAPI()
    app.include_router(telemetry_router.router)

    async def override_get_session():
        async with sm() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


async def _seed_router(sm, *, status: str = "active") -> tuple[int, str]:
    """Insert a Router + RouterToken row. Return (router_id, raw_token)."""
    async with sm() as s:
        router_row = Router(
            identity="hAP ac2 - Test, Bench",
            serial_number="HC_TEL_001",
            mac_address="02:00:00:00:00:01",
            model="hAP ac2",
            ros_version="7.14.2",
            ros_major=7,
            wifi_stack="wireless",
            wg_public_key="xTIBA9rboUdnM3HNyLwxcOhVmUiDHvjvrE1nMAIv+XI=",
            wg_overlay_ip="10.100.0.2",
            enrolled_at=datetime.now(timezone.utc),
            status=status,
        )
        s.add(router_row)
        await s.flush()
        raw = mint_telemetry_token()
        s.add(
            RouterToken(
                router_id=router_row.id,
                token_hash=hash_token(raw),
                token_prefix=prefix_of(raw),
            )
        )
        await s.commit()
        return router_row.id, raw


def _heartbeat(**overrides) -> dict:
    base = {
        "identity": "hAP ac2 - Test, Bench",
        "uptime": "1h23m",
        "wifi_stack": "wireless",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_missing_authorization_returns_401(client):
    r = client.post("/api/v1/telemetry", json=_heartbeat())
    assert r.status_code == 401
    assert "missing Authorization header" in r.json()["detail"]


def test_malformed_authorization_returns_401(client):
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": "Basic abc123"},
    )
    assert r.status_code == 401
    assert "malformed Authorization header" in r.json()["detail"]


def test_unknown_token_returns_401(client):
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": "Bearer not-a-real-token-at-all"},
    )
    assert r.status_code == 401
    assert "invalid or revoked" in r.json()["detail"]


def test_revoked_token_returns_401(client, sm):
    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    async def revoke():
        async with sm() as s:
            tok = (await s.scalars(select(RouterToken))).one()
            tok.revoked_at = datetime.now(timezone.utc)
            await s.commit()

    asyncio.get_event_loop().run_until_complete(revoke())

    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_token_updates_last_seen_and_returns_204(client, sm):
    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    # Precondition: last_seen_at is None after seed
    async def check_pre():
        async with sm() as s:
            row = await s.get(Router, router_id)
            assert row.last_seen_at is None

    asyncio.get_event_loop().run_until_complete(check_pre())

    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert r.content == b""

    async def check_post():
        async with sm() as s:
            row = await s.get(Router, router_id)
            assert row.last_seen_at is not None
            # Fresh timestamp (within the last few seconds)
            delta = datetime.now(timezone.utc) - row.last_seen_at
            assert delta.total_seconds() < 5

    asyncio.get_event_loop().run_until_complete(check_post())


def test_pending_router_still_accepts_telemetry(client, sm):
    """Pending routers still send heartbeats — admin approval is the only
    gate that changes; liveness tracking matters while they wait."""
    _, raw = asyncio.get_event_loop().run_until_complete(
        _seed_router(sm, status="pending")
    )
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text


def test_extra_fields_are_allowed(client, sm):
    """Future telemetry templates can add fields without breaking old deploys."""
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    payload = _heartbeat(
        registration_table=[{"mac": "AA:BB:CC:DD:EE:FF", "signal": -62}],
        unexpected_new_field="that's fine",
    )
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text


# ---------------------------------------------------------------------------
# Router state gate
# ---------------------------------------------------------------------------

def test_decommissioned_router_returns_403(client, sm):
    _, raw = asyncio.get_event_loop().run_until_complete(
        _seed_router(sm, status="decommissioned")
    )
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 403
    assert "decommissioned" in r.json()["detail"]


def test_quarantined_router_returns_403(client, sm):
    _, raw = asyncio.get_event_loop().run_until_complete(
        _seed_router(sm, status="quarantined")
    )
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 403
    assert "quarantined" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

def test_malformed_payload_returns_422(client, sm):
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    # wifi_stack must match ^(wireless|wifi)$
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(wifi_stack="mesh"),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


def test_missing_required_field_returns_422(client, sm):
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    payload = _heartbeat()
    del payload["uptime"]
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

def test_rate_limit_triggers_429(client, sm):
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    headers = {"Authorization": f"Bearer {raw}"}
    # Bucket capacity is 10 per (scope, key). Both IP and router_id scopes
    # use the same key for all requests here, so the 11th call exhausts
    # whichever bucket refills slower (both are 10/min).
    for _ in range(10):
        r = client.post("/api/v1/telemetry", json=_heartbeat(), headers=headers)
        assert r.status_code == 204
    r = client.post("/api/v1/telemetry", json=_heartbeat(), headers=headers)
    assert r.status_code == 429
    assert "rate limit" in r.json()["detail"]
