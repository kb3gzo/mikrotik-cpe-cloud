"""Integration tests for POST /api/v1/telemetry.

Covers: auth (missing/malformed/unknown/revoked bearer), router state gate
(active/pending OK, decommissioned/quarantined rejected), liveness update,
payload validation (Phase 1 flat and Chunk A nested shapes), rate limiting,
AND the Influx writer handshake (Task #22) -- called with the right args on
success, and its failure never breaks the 204 response path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

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
            # SQLite drops tzinfo on round-trip even with DateTime(timezone=True);
            # Postgres TIMESTAMPTZ preserves it. Re-attach UTC if missing so the
            # test passes against either backend.
            last_seen = row.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - last_seen
            assert delta.total_seconds() < 5

    asyncio.get_event_loop().run_until_complete(check_post())


def test_pending_router_still_accepts_telemetry(client, sm):
    """Pending routers still send heartbeats -- admin approval is the only
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
        unexpected_new_field="thats fine",
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


# ---------------------------------------------------------------------------
# Influx writer integration (Task #22)
# ---------------------------------------------------------------------------

def test_happy_path_invokes_influx_writer(client, sm, monkeypatch):
    """On 204, write_telemetry should be called once with the router row
    and the parsed payload dict. This is the contract Deliverable #8
    depends on -- it will pass a richer payload through the same seam."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(uptime="1h5m"),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1

    router_arg, payload_arg = spy.await_args.args
    assert router_arg.id == router_id
    assert payload_arg["uptime"] == "1h5m"
    assert payload_arg["identity"] == "hAP ac2 - Test, Bench"
    assert payload_arg["wifi_stack"] == "wireless"


def test_influx_write_failure_still_returns_204(client, sm, monkeypatch):
    """Design Section 5.3: Influx being down must not break the 204 path.
    The outer try/except in the handler is defense-in-depth for the case
    where a future write_telemetry refactor lets an exception escape."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated influx outage")

    monkeypatch.setattr("app.routers.telemetry.write_telemetry", boom)

    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text

    # Liveness was committed BEFORE the Influx call; must still be set
    # even though the writer raised.
    async def check():
        async with sm() as s:
            row = await s.get(Router, router_id)
            assert row.last_seen_at is not None

    asyncio.get_event_loop().run_until_complete(check())


# ---------------------------------------------------------------------------
# Deliverable #8 Chunk A: expanded payload shape
# ---------------------------------------------------------------------------

def test_chunk_a_full_payload_accepted_and_forwarded(client, sm, monkeypatch):
    """POST the full expanded shape (top-level identity + nested system
    block) and verify the handler returns 204 and passes the full structure
    through to write_telemetry unchanged.

    This is the contract the new telemetry-{wireless,wifi}.rsc.j2 templates
    emit -- if this test passes, the server can safely receive a payload
    from a re-provisioned router running the Chunk A script.
    """
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    payload = {
        "schema_version": 1,
        "identity": "hAP ac2 - Test, Bench",
        "serial": "HC_TEL_001",
        "mac": "02:00:00:00:00:01",
        "board": "RB952Ui-5ac2nD",
        "ros_version": "7.14.2",
        "wifi_stack": "wireless",
        "system": {
            "uptime": "2h15m",
            "cpu_load_pct": 4,
            "free_memory_bytes": 56213504,
            "total_memory_bytes": 134217728,
            # hAP ac2 has no temperature / voltage probe -- omitted on purpose
        },
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1

    router_arg, payload_arg = spy.await_args.args
    assert router_arg.id == router_id
    # Top-level identity fields flow through
    assert payload_arg["serial"] == "HC_TEL_001"
    assert payload_arg["mac"] == "02:00:00:00:00:01"
    assert payload_arg["board"] == "RB952Ui-5ac2nD"
    assert payload_arg["ros_version"] == "7.14.2"
    assert payload_arg["schema_version"] == 1
    # Nested system block is preserved as a dict (model_dump default)
    sys_arg = payload_arg["system"]
    assert sys_arg["uptime"] == "2h15m"
    assert sys_arg["cpu_load_pct"] == 4
    assert sys_arg["free_memory_bytes"] == 56213504
    assert sys_arg["total_memory_bytes"] == 134217728
    # Absent sensors stay absent (None is fine -- the writer skips them)
    assert sys_arg.get("temperature_c") is None
    assert sys_arg.get("voltage_v") is None


def test_chunk_a_with_sensor_readings_passes_through(client, sm, monkeypatch):
    """hAP ax3 / ax2 DO have temperature + voltage probes -- the handler
    must accept those optional floats and forward them to the writer."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    payload = {
        "schema_version": 1,
        "identity": "hAP ax3 - Test, Bench",
        "wifi_stack": "wifi",
        "system": {
            "uptime": "3d4h",
            "cpu_load_pct": 12,
            "free_memory_bytes": 400000000,
            "total_memory_bytes": 1073741824,
            "temperature_c": 43.5,
            "voltage_v": 23.9,
        },
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1
    _, payload_arg = spy.await_args.args
    assert payload_arg["system"]["temperature_c"] == 43.5
    assert payload_arg["system"]["voltage_v"] == 23.9


def test_chunk_a_rejects_bad_mac_format(client, sm):
    """Defence against typoed templates: malformed MAC must 422, not
    silently store junk."""
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    payload = _heartbeat(mac="not-a-mac-address")
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


def test_chunk_a_rejects_out_of_range_sensor(client, sm):
    """Sensor bounds catch unit-confusion bugs (e.g. raw ADC values in
    the thousands being dumped into temperature_c)."""
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    payload = {
        "identity": "hAP ax3 - Test, Bench",
        "wifi_stack": "wifi",
        "uptime": "1h",
        "system": {
            "uptime": "1h",
            "temperature_c": 9999,  # above 120 cap
        },
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


def test_phase1_flat_shape_still_accepted(client, sm, monkeypatch):
    """Rolling deploy guarantee: a router still running the Phase 1 flat
    template (no ``system`` block, uptime at top level) must keep working
    until it is re-provisioned. The Chunk A schema kept every new field
    optional specifically to preserve this."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    # Phase 1 shape: just identity, uptime, wifi_stack.
    r = client.post(
        "/api/v1/telemetry",
        json={
            "identity": "hAP ac2 - Test, Bench",
            "uptime": "5m23s",
            "wifi_stack": "wireless",
        },
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1
    _, payload_arg = spy.await_args.args
    assert payload_arg["uptime"] == "5m23s"
    # No system block was sent -- dump preserves it as None
    assert payload_arg.get("system") is None


# ---------------------------------------------------------------------------
# Deliverable #8 Chunk B: per-interface arrays
# ---------------------------------------------------------------------------

def test_chunk_b_classic_shape_with_interfaces(client, sm, monkeypatch):
    """Classic-stack heartbeat: full Chunk A + Chunk B payload with both
    ethernet[] and wireless_interfaces[] arrays. Asserts 204 + the writer
    receives the lists intact (each entry carries name + counters).
    """
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    router_id, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    payload = {
        "schema_version": 1,
        "identity": "hAP ac2 - Test, Bench",
        "serial": "HC_TEL_001",
        "mac": "02:00:00:00:00:01",
        "board": "RB952Ui-5ac2nD",
        "ros_version": "7.14.2",
        "wifi_stack": "wireless",
        "system": {
            "uptime": "2h15m",
            "cpu_load_pct": 4,
            "free_memory_bytes": 56213504,
            "total_memory_bytes": 134217728,
        },
        "ethernet": [
            {"name": "ether1", "running": True,
             "rx_bytes": 12345, "tx_bytes": 67890,
             "rx_packets": 100, "tx_packets": 200},
            {"name": "ether2", "running": False,
             "rx_bytes": 0, "tx_bytes": 0,
             "rx_packets": 0, "tx_packets": 0},
        ],
        "wireless_interfaces": [
            {"name": "wlan1", "ssid": "Smith-2G", "band": "2ghz-b/g/n",
             "frequency": 2412, "channel_width": "20mhz",
             "tx_power": "default", "disabled": False, "mode": "ap-bridge",
             "rx_bytes": 1000, "tx_bytes": 2000,
             "rx_packets": 10, "tx_packets": 20},
            {"name": "wlan2", "ssid": "Smith-5G", "band": "5ghz-ac",
             "frequency": 5220, "channel_width": "20/40/80mhz-XXXX",
             "tx_power": "23", "disabled": False, "mode": "ap-bridge",
             "rx_bytes": 5000, "tx_bytes": 9000,
             "rx_packets": 50, "tx_packets": 90},
        ],
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1

    router_arg, payload_arg = spy.await_args.args
    assert router_arg.id == router_id
    # Lists preserved through Pydantic round-trip
    assert len(payload_arg["ethernet"]) == 2
    assert len(payload_arg["wireless_interfaces"]) == 2
    assert payload_arg["ethernet"][0]["name"] == "ether1"
    assert payload_arg["ethernet"][0]["rx_bytes"] == 12345
    assert payload_arg["wireless_interfaces"][1]["ssid"] == "Smith-5G"
    assert payload_arg["wireless_interfaces"][1]["frequency"] == 5220
    # Wave2-only field stays absent on the classic payload
    assert payload_arg.get("wifi_interfaces") is None


def test_chunk_b_wave2_shape_with_interfaces(client, sm, monkeypatch):
    """Wave2-stack heartbeat: ethernet[] + wifi_interfaces[]. The
    wireless_interfaces[] array must NOT appear -- a wave2 router never
    populates it."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    payload = {
        "identity": "hAP ax3 - Test, Bench",
        "wifi_stack": "wifi",
        "system": {"uptime": "3d4h", "cpu_load_pct": 12,
                   "free_memory_bytes": 400000000,
                   "total_memory_bytes": 1073741824},
        "ethernet": [
            {"name": "ether1", "running": True,
             "rx_bytes": 1, "tx_bytes": 2,
             "rx_packets": 3, "tx_packets": 4},
        ],
        "wifi_interfaces": [
            {"name": "wifi1", "ssid": "Smith-AX-2G",
             "channel": "2412/20mhz", "disabled": False,
             "configuration": "ap-cfg-2g",
             "rx_bytes": 100, "tx_bytes": 200,
             "rx_packets": 1, "tx_packets": 2},
            {"name": "wifi2", "ssid": "Smith-AX-5G",
             "channel": "5180/20mhz", "disabled": False,
             "configuration": "ap-cfg-5g",
             "rx_bytes": 1000, "tx_bytes": 2000,
             "rx_packets": 10, "tx_packets": 20},
        ],
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1

    _, payload_arg = spy.await_args.args
    assert len(payload_arg["wifi_interfaces"]) == 2
    assert payload_arg["wifi_interfaces"][0]["channel"] == "2412/20mhz"
    assert payload_arg.get("wireless_interfaces") is None


def test_chunk_b_rejects_negative_counter(client, sm):
    """Counters must be >=0 -- a negative byte count is RouterOS noise we
    don't want to ingest. Pydantic ge=0 catches it pre-write."""
    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))
    payload = {
        "identity": "hAP ac2 - Test, Bench",
        "wifi_stack": "wireless",
        "uptime": "1h",
        "ethernet": [
            {"name": "ether1", "running": True, "rx_bytes": -5,
             "tx_bytes": 1, "rx_packets": 0, "tx_packets": 0},
        ],
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


def test_chunk_b_extra_fields_allowed_for_forward_compat(client, sm, monkeypatch):
    """extra="allow" on the interface models means a future RouterOS field
    (e.g. tx_power_dbm migrated from string to int, or new tx_errors counter)
    won't 422 the payload during the rolling deploy window."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    _, raw = asyncio.get_event_loop().run_until_complete(_seed_router(sm))

    payload = {
        "identity": "hAP ac2 - Test, Bench",
        "wifi_stack": "wireless",
        "uptime": "1h",
        "wireless_interfaces": [
            {"name": "wlan1", "disabled": False,
             "rx_bytes": 1, "tx_bytes": 2,
             "rx_packets": 3, "tx_packets": 4,
             # Hypothetical future fields:
             "tx_errors": 7,
             "noise_floor_dbm": -95},
        ],
    }
    r = client.post(
        "/api/v1/telemetry",
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 204, r.text
    assert spy.await_count == 1
    _, payload_arg = spy.await_args.args
    # Extra fields preserved through model_dump
    assert payload_arg["wireless_interfaces"][0]["tx_errors"] == 7
    assert payload_arg["wireless_interfaces"][0]["noise_floor_dbm"] == -95


def test_influx_writer_not_called_on_auth_failure(client, sm, monkeypatch):
    """No Influx write attempt when the request is rejected at the auth
    layer -- saves a pointless client call per scanner hit."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": "Bearer definitely-not-real"},
    )
    assert r.status_code == 401
    assert spy.await_count == 0


def test_influx_writer_not_called_on_decommissioned_router(client, sm, monkeypatch):
    """403 gate runs before the writer; a quarantined/decommissioned
    router should NOT generate Influx points."""
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.telemetry.write_telemetry", spy)

    _, raw = asyncio.get_event_loop().run_until_complete(
        _seed_router(sm, status="decommissioned")
    )
    r = client.post(
        "/api/v1/telemetry",
        json=_heartbeat(),
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 403
    assert spy.await_count == 0
