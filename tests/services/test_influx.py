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


# ---------------------------------------------------------------------------
# _build_interface_points (Chunk B)
# ---------------------------------------------------------------------------

def _ts():
    return datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_build_interface_points_empty_payload():
    """No interface arrays in payload -> empty list, no exceptions."""
    points = influx._build_interface_points(_fake_router(), {}, _ts())
    assert points == []


def test_build_interface_points_ignores_non_list_fields():
    """Defensive: a malformed payload where ``ethernet`` is a dict (not a
    list) shouldn't crash -- just gets skipped."""
    payload = {"ethernet": {"name": "ether1"}, "wireless_interfaces": "nope"}
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert points == []


def test_build_interface_points_ethernet_single():
    """One ether entry -> one point with kind=ethernet and counter fields."""
    payload = {
        "ethernet": [
            {
                "name": "ether1",
                "running": True,
                "rx_bytes": 12345,
                "tx_bytes": 67890,
                "rx_packets": 100,
                "tx_packets": 200,
            }
        ]
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 1
    line = points[0].to_line_protocol()
    assert line.startswith("interface,")
    # Tags
    assert "interface_name=ether1" in line
    assert "kind=ethernet" in line
    assert "router_id=42" in line
    # Fields -- ints carry the i suffix, bool runs as raw true/false
    assert "rx_bytes=12345i" in line
    assert "tx_bytes=67890i" in line
    assert "rx_packets=100i" in line
    assert "tx_packets=200i" in line
    assert "running=true" in line
    # Wireless-only fields must not leak in
    assert "ssid" not in line
    assert "frequency" not in line


def test_build_interface_points_wireless_with_metadata():
    """Wireless entry tags as kind=wireless and pulls metadata strings into
    fields; bool ``disabled`` becomes a bool field."""
    payload = {
        "wireless_interfaces": [
            {
                "name": "wlan1",
                "ssid": "Smith-5G",
                "band": "5ghz-ac",
                "frequency": 5220,
                "channel_width": "20/40/80mhz-XXXX",
                "tx_power": "23",
                "disabled": False,
                "mode": "ap-bridge",
                "rx_bytes": 1000,
                "tx_bytes": 2000,
                "rx_packets": 10,
                "tx_packets": 20,
            }
        ]
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 1
    line = points[0].to_line_protocol()
    assert "kind=wireless" in line
    assert "interface_name=wlan1" in line
    # frequency is the only int wireless metadata field
    assert "frequency=5220i" in line
    # String fields (line-protocol escapes spaces with a backslash)
    assert 'ssid="Smith-5G"' in line
    assert 'band="5ghz-ac"' in line
    assert 'tx_power="23"' in line
    assert 'mode="ap-bridge"' in line
    assert "disabled=false" in line


def test_build_interface_points_wifi_wave2():
    """Wave2 entry uses kind=wifi and the channel/configuration fields."""
    payload = {
        "wifi_interfaces": [
            {
                "name": "wifi1",
                "ssid": "Smith-AX",
                "channel": "5180/20mhz",
                "disabled": False,
                "configuration": "ap-cfg",
                "rx_bytes": 500,
                "tx_bytes": 1500,
            }
        ]
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 1
    line = points[0].to_line_protocol()
    assert "kind=wifi" in line
    assert "interface_name=wifi1" in line
    assert 'ssid="Smith-AX"' in line
    assert 'channel="5180/20mhz"' in line
    assert 'configuration="ap-cfg"' in line
    assert "rx_bytes=500i" in line
    assert "disabled=false" in line
    # Wireless-only fields shouldn't leak from wifi
    assert "frequency" not in line
    assert "band" not in line
    assert "mode" not in line


def test_build_interface_points_skips_nameless_entries():
    """An entry without a name is skipped rather than emitted with a blank
    interface_name tag (which would alias every nameless interface across
    the fleet)."""
    payload = {
        "ethernet": [
            {"running": True, "rx_bytes": 1, "tx_bytes": 2},  # no name
            {"name": "ether2", "running": True, "rx_bytes": 3, "tx_bytes": 4},
        ]
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 1
    assert "interface_name=ether2" in points[0].to_line_protocol()


def test_build_interface_points_drops_field_only_entry():
    """An entry with only a name and no fields would be a tag-only point,
    which Influx rejects. The builder must drop it instead."""
    payload = {"ethernet": [{"name": "ether3"}]}
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert points == []


def test_build_interface_points_full_classic_shape():
    """Classic-stack heartbeat: ethernet + wireless together produce one
    point per interface, ordered as encountered in the payload."""
    payload = {
        "ethernet": [
            {"name": "ether1", "running": True, "rx_bytes": 1, "tx_bytes": 2,
             "rx_packets": 3, "tx_packets": 4},
            {"name": "ether2", "running": False, "rx_bytes": 0, "tx_bytes": 0,
             "rx_packets": 0, "tx_packets": 0},
        ],
        "wireless_interfaces": [
            {"name": "wlan1", "disabled": False, "rx_bytes": 100,
             "tx_bytes": 200, "rx_packets": 1, "tx_packets": 2,
             "ssid": "S1", "band": "5ghz-ac", "frequency": 5220},
        ],
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 3
    kinds = [p.to_line_protocol().split(",")[1:3] for p in points]
    # First two are ethernet, third is wireless
    flat = [seg for tags in kinds for seg in tags]
    assert sum("kind=ethernet" in s for s in flat) == 2
    assert sum("kind=wireless" in s for s in flat) == 1


def test_build_interface_points_full_wave2_shape():
    """Wave2-stack heartbeat: ethernet + wifi (no wireless_interfaces)."""
    payload = {
        "ethernet": [
            {"name": "ether1", "running": True, "rx_bytes": 1, "tx_bytes": 2,
             "rx_packets": 3, "tx_packets": 4},
        ],
        "wifi_interfaces": [
            {"name": "wifi1", "disabled": False, "rx_bytes": 50,
             "tx_bytes": 75, "ssid": "AX-2G", "channel": "2412/20mhz"},
            {"name": "wifi2", "disabled": False, "rx_bytes": 500,
             "tx_bytes": 750, "ssid": "AX-5G", "channel": "5180/20mhz"},
        ],
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 3
    lines = [p.to_line_protocol() for p in points]
    assert any("kind=ethernet" in l and "interface_name=ether1" in l for l in lines)
    assert any("kind=wifi" in l and "interface_name=wifi1" in l for l in lines)
    assert any("kind=wifi" in l and "interface_name=wifi2" in l for l in lines)


def test_build_interface_points_drops_non_int_counter():
    """A counter that arrives as a non-numeric string (rare RouterOS quirk)
    is dropped silently rather than crashing the whole heartbeat write."""
    payload = {
        "ethernet": [
            {"name": "ether1", "running": True,
             "rx_bytes": "garbage", "tx_bytes": 1234,
             "rx_packets": 1, "tx_packets": 2},
        ]
    }
    points = influx._build_interface_points(_fake_router(), payload, _ts())
    assert len(points) == 1
    line = points[0].to_line_protocol()
    assert "rx_bytes" not in line  # dropped
    assert "tx_bytes=1234i" in line  # kept
    assert "running=true" in line
