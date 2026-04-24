"""Unit tests for ``app.services.influx``.

Focus is on the pure bits that don't touch a real Influx: the uptime
parser, the point builder, and the payload-shape extractor. The client
lifecycle and ``write_telemetry`` behaviour under Influx failure are
covered by the handler integration tests in ``tests/routers/test_telemetry.py``
(which monkeypatch the writer anyway).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import influx


# ---------------------------------------------------------------------------
# _parse_uptime
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "s,expected",
    [
        # Typical short uptimes
        ("5s", 5),
        ("23m12s", 23 * 60 + 12),
        ("1h", 3600),
        ("1h5s", 3605),
        # Full format including weeks
        ("1w2d3h4m5s", 7 * 86400 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ("2w", 2 * 7 * 86400),
        # Leading zeros are fine -- RouterOS doesn't usually emit them but be lenient
        ("0h0m30s", 30),
        # Whitespace tolerated (RouterOS sometimes pads)
        ("  1h  ", 3600),
    ],
)
def test_parse_uptime_valid_formats(s, expected):
    assert influx._parse_uptime(s) == expected


@pytest.mark.parametrize(
    "s",
    [
        "",
        "garbage",
        "1y2z",  # unknown units
        "3600",  # bare seconds without unit -- we REQUIRE unit suffixes
        "1h2x",  # partial parse with trailing junk
        None,    # robust against accidental None
    ],
)
def test_parse_uptime_unparseable_returns_zero(s):
    # Pass None explicitly via a guard since the type hint is str
    if s is None:
        assert influx._parse_uptime("") == 0
    else:
        assert influx._parse_uptime(s) == 0


# ---------------------------------------------------------------------------
# _build_system_point
# ---------------------------------------------------------------------------

def _fake_router(**overrides):
    """Build a Router-shaped object with only the fields _build_system_point reads."""
    defaults = dict(
        id=42,
        model="hAP ac2",
        wifi_stack="wireless",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_build_system_point_phase1_shape_still_works():
    """A payload with no ``system`` block (Phase 1 heartbeat) must still
    produce a valid point with just ``uptime_sec`` -- no crashes on the
    optional field loop."""
    ts = datetime(2026, 4, 23, 18, 50, 47, tzinfo=timezone.utc)
    p = influx._build_system_point(_fake_router(), uptime_sec=3661, system=None, ts=ts)

    line = p.to_line_protocol()
    assert line.startswith("system,")
    # Tags (alphabetised by the client)
    assert "model=hAP\\ ac2" in line  # space is escaped
    assert "router_id=42" in line
    assert "wifi_stack=wireless" in line
    # Field
    assert "uptime_sec=3661i" in line
    # No Chunk A fields should leak into the line when system=None
    assert "cpu_load_pct" not in line
    assert "temperature_c" not in line


def test_build_system_point_handles_missing_router_fields():
    """A router with NULL model / wifi_stack shouldn't tag 'None'."""
    r = _fake_router(model=None, wifi_stack=None)
    p = influx._build_system_point(
        r, uptime_sec=0, system=None, ts=datetime.now(timezone.utc)
    )
    line = p.to_line_protocol()
    assert "model=unknown" in line
    assert "wifi_stack=unknown" in line


def test_build_system_point_emits_chunk_a_fields_when_present():
    """Chunk A: cpu/memory/temperature/voltage go into the point when sent."""
    ts = datetime(2026, 4, 23, 18, 50, 47, tzinfo=timezone.utc)
    system = {
        "uptime": "1h",
        "cpu_load_pct": 8,
        "free_memory_bytes": 56213504,
        "total_memory_bytes": 134217728,
        "temperature_c": 42.0,
        "voltage_v": 24.1,
    }
    p = influx._build_system_point(_fake_router(), uptime_sec=3600, system=system, ts=ts)
    line = p.to_line_protocol()
    # Integer fields carry the Influx 'i' suffix
    assert "cpu_load_pct=8i" in line
    assert "free_memory_bytes=56213504i" in line
    assert "total_memory_bytes=134217728i" in line
    # Floats don't
    assert "temperature_c=42" in line
    assert "voltage_v=24.1" in line


def test_build_system_point_skips_absent_sensors():
    """hAP ac2 has no temperature/voltage probes -- those fields must not be
    written as 0, because 0 conflates 'no sensor' with 'sensor reads zero'."""
    ts = datetime(2026, 4, 23, 18, 50, 47, tzinfo=timezone.utc)
    system = {
        "uptime": "5m",
        "cpu_load_pct": 1,
        "free_memory_bytes": 50_000_000,
        "total_memory_bytes": 134_217_728,
        # no temperature_c, no voltage_v
    }
    p = influx._build_system_point(_fake_router(), uptime_sec=300, system=system, ts=ts)
    line = p.to_line_protocol()
    assert "cpu_load_pct=1i" in line
    assert "temperature_c" not in line
    assert "voltage_v" not in line


# ---------------------------------------------------------------------------
# _extract_uptime_string -- prefers nested system.uptime over flat uptime
# ---------------------------------------------------------------------------

def test_extract_uptime_prefers_nested_over_flat():
    """Chunk A shape wins when both are present (rolling-deploy safety)."""
    payload = {"uptime": "flat-value", "system": {"uptime": "1h5m"}}
    assert influx._extract_uptime_string(payload) == "1h5m"


def test_extract_uptime_falls_back_to_flat():
    """Phase 1 heartbeat (no system block) still parses."""
    payload = {"uptime": "23m"}
    assert influx._extract_uptime_string(payload) == "23m"


def test_extract_uptime_empty_when_neither():
    assert influx._extract_uptime_string({}) == ""


def test_extract_uptime_ignores_non_dict_system():
    """Defensive: a malformed 'system' field that isn't a dict shouldn't crash."""
    payload = {"uptime": "1h", "system": "not-a-dict"}
    assert influx._extract_uptime_string(payload) == "1h"


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_client_returns_none_when_no_token(monkeypatch):
    """No INFLUX_TOKEN configured -> client disabled, writes become no-ops."""
    # Reset the singleton so the test runs fresh
    monkeypatch.setattr(influx, "_client", None)

    from app.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("INFLUX_TOKEN", "")

    client = await influx.get_client()
    assert client is None

    # Cleanup -- don't leak test env into later tests
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_write_telemetry_noop_when_client_none(monkeypatch):
    """write_telemetry should return quietly if no client is configured."""

    async def fake_get_client():
        return None

    monkeypatch.setattr(influx, "get_client", fake_get_client)
    # Should NOT raise, regardless of payload contents
    await influx.write_telemetry(_fake_router(), {"uptime": "1h"})
