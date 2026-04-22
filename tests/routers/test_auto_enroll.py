"""Integration tests for POST /api/v1/auto-enroll.

We spin up a FastAPI TestClient with the DB dependency swapped for an async
SQLite session and the wireguard service patched so we don't actually try to
rewrite /etc/wireguard/wg0.conf during tests.

These tests focus on the *handler's* behaviour (auth, rate-limit, row
updates, template render). wireguard.sync_from_db has its own unit tests in
tests/services/test_wireguard.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import get_session
from app.models import AuditLog, Router, RouterToken
from app.models.base import Base
from app.routers import auto_enroll
from app.services import wireguard as wg_service
from app.services.rate_limit import _buckets  # noqa: used to reset between tests
from app.services.tokens import hash_token, prefix_of


VALID_PUBKEY = "xTIBA9rboUdnM3HNyLwxcOhVmUiDHvjvrE1nMAIv+XI="
SECRET_CURRENT = "TEST_CURRENT_SECRET_ABC"
SECRET_PREVIOUS = "TEST_PREVIOUS_SECRET_XYZ"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sm():
    """Sessionmaker bound to an in-memory SQLite DB.

    SQLite can't do INET / MACADDR / JSONB / pg_trgm - so we only create the
    specific tables our tests touch, and rely on SQLAlchemy falling back to
    TEXT for the unusual column types.

    In practice we create routers + router_tokens + audit_log only.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        # Turn off GIN/JSONB-using indexes that SQLite can't understand
    )

    async with engine.begin() as conn:
        # Force INET/MACADDR/JSONB to behave as TEXT in the sqlite dialect
        # by creating tables with SQLAlchemy's column-level compile rules.
        # The simplest path: use create_all on just the tables we need.
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc,
                tables=[
                    Router.__table__,
                    RouterToken.__table__,
                    AuditLog.__table__,
                ],
                checkfirst=True,
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Give every test a fresh rate-limit bucket map."""
    _buckets.clear()
    yield
    _buckets.clear()


@pytest.fixture
def settings_override() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        provisioning_secret_current=SECRET_CURRENT,
        provisioning_secret_previous=SECRET_PREVIOUS,
        server_fqdn="test.example.com",
        wg_server_public_key="TESTSERVERPUBKEYBASE64EXAMPLEAAAAAAAAAAAAAAA=",
        wg_server_ip="10.100.0.1",
        wg_overlay_cidr="10.100.0.0/22",
    )


@pytest.fixture
def app(sm, settings_override, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(auto_enroll.router)

    async def override_get_session():
        async with sm() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: settings_override
    # The handler calls get_settings() directly (not via Depends), so patch
    # the module references too. wg_service.get_server_public_key() also
    # reads settings directly, so patch it there as well — otherwise it
    # falls through to the real lru_cache'd .env value.
    monkeypatch.setattr(auto_enroll, "get_settings", lambda: settings_override)
    monkeypatch.setattr(wg_service, "get_settings", lambda: settings_override)

    # Don't actually touch /etc/wireguard/wg0.conf
    async def fake_sync(session):
        return {"peers_synced": 1, "config_path": "/tmp/fake-wg0.conf"}

    monkeypatch.setattr(
        wg_service, "sync_from_db", AsyncMock(side_effect=fake_sync)
    )
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _payload(**overrides) -> dict:
    base = {
        "serial": "HC1234567A",
        "mac": "CC:2D:E0:12:34:56",
        "model": "hAP ac lite",
        "identity": "hAP ac lite - Smith, John",
        "ros_version": "7.14.2",
        "wifi_stack": "wireless",
        "router_public_key": VALID_PUBKEY,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_missing_secret_is_401_rsc(client):
    r = client.post("/api/v1/auto-enroll", json=_payload())
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/plain")
    assert ":log error" in r.text
    assert "missing provisioning secret" in r.text


def test_bad_secret_is_401_rsc(client):
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(),
        headers={"X-Provisioning-Secret": "not-the-real-secret"},
    )
    assert r.status_code == 401
    assert "invalid provisioning secret" in r.text


def test_current_secret_succeeds(client, sm):
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r.status_code == 200, r.text
    body = r.text
    # Template rendered with expected pieces
    assert "address=10.100.0.2/32" in body  # first host after .1 (server)
    assert "TESTSERVERPUBKEYBASE64EXAMPLEAAAAAAAAAAAAAAA=" in body
    assert "endpoint-address=test.example.com" in body
    assert "endpoint-port=51820" in body
    assert "allowed-address=10.100.0.0/22" in body
    assert "src-address=10.100.0.1" in body  # firewall accept from server
    assert "cpe-cloud-enrolled.flag" in body
    # Telemetry script embedded
    assert "cpe-cloud-telemetry" in body
    assert "https://test.example.com/api/v1/telemetry" in body

    # Router row + telemetry token persisted
    async def check():
        async with sm() as s:
            router_row = (await s.scalars(select(Router))).one()
            assert router_row.serial_number == "HC1234567A"
            # identity matched the auto-approve regex
            assert router_row.status == "active"
            assert router_row.wg_public_key == VALID_PUBKEY
            tokens = (await s.scalars(select(RouterToken))).all()
            assert len(tokens) == 1
            assert tokens[0].revoked_at is None
            audits = (await s.scalars(select(AuditLog))).all()
            assert any(a.action == "auto_enroll_new" for a in audits)

    asyncio.get_event_loop().run_until_complete(check())


def test_previous_secret_also_succeeds(client):
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(serial="HC9999999Z", identity="hAP ac - Other, Serial"),
        headers={"X-Provisioning-Secret": SECRET_PREVIOUS},
    )
    assert r.status_code == 200, r.text


def test_non_matching_identity_lands_pending(client, sm):
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(
            serial="HC0000000B",
            identity="MikroTik",  # doesn't match hAP <model> - <Last, First>
        ),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r.status_code == 200

    async def check():
        async with sm() as s:
            router_row = (
                await s.scalars(
                    select(Router).where(Router.serial_number == "HC0000000B")
                )
            ).one()
            assert router_row.status == "pending"

    asyncio.get_event_loop().run_until_complete(check())


def test_reenrollment_keeps_overlay_ip_rotates_key(client, sm):
    # First enroll
    r1 = client.post(
        "/api/v1/auto-enroll",
        json=_payload(serial="HC_REENROLL", identity="hAP ac lite - Re, Enroll"),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r1.status_code == 200

    # Second enroll with a DIFFERENT pubkey
    new_pubkey = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    r2 = client.post(
        "/api/v1/auto-enroll",
        json=_payload(
            serial="HC_REENROLL",
            identity="hAP ac lite - Re, Enroll",
            router_public_key=new_pubkey,
        ),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r2.status_code == 200, r2.text

    async def check():
        async with sm() as s:
            rows = (
                await s.scalars(
                    select(Router).where(Router.serial_number == "HC_REENROLL")
                )
            ).all()
            assert len(rows) == 1  # updated in place
            assert rows[0].wg_public_key == new_pubkey
            tokens = (
                await s.scalars(
                    select(RouterToken).where(RouterToken.router_id == rows[0].id)
                )
            ).all()
            # Two tokens total: old one revoked, new one active
            assert len(tokens) == 2
            active = [t for t in tokens if t.revoked_at is None]
            revoked = [t for t in tokens if t.revoked_at is not None]
            assert len(active) == 1
            assert len(revoked) == 1

    asyncio.get_event_loop().run_until_complete(check())


def test_invalid_payload_returns_422(client):
    # mac too short
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(mac="CC:2D"),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r.status_code == 422


def test_rate_limit_triggers_429(client):
    # Both serial and pubkey have UNIQUE constraints, so we have to vary
    # both per iteration - otherwise insert #2 collides on wg_public_key
    # before we ever reach the rate-limit guard.
    def _unique_pubkey(i):
        base = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 40 A's
        return f"{base}{i:03d}="  # total 44 chars, unique per i

    for i in range(10):
        client.post(
            "/api/v1/auto-enroll",
            json=_payload(
                serial=f"HC_RATE_{i}",
                router_public_key=_unique_pubkey(i),
            ),
            headers={"X-Provisioning-Secret": SECRET_CURRENT},
        )
    # 11th request from same IP should rate-limit
    r = client.post(
        "/api/v1/auto-enroll",
        json=_payload(
            serial="HC_RATE_11",
            router_public_key=_unique_pubkey(11),
        ),
        headers={"X-Provisioning-Secret": SECRET_CURRENT},
    )
    assert r.status_code == 429
    assert "rate limit" in r.text
