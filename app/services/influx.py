"""InfluxDB writer for telemetry heartbeats.

Design Section 5.4 defines three measurements -- ``system``, ``interface``,
and ``client``. Phase 1 wrote only ``system`` with ``uptime_sec``. Chunk A
expanded ``system`` with cpu/memory/temperature/voltage. Chunk B (this
module's current state) adds the ``interface`` measurement -- one point per
ethernet/wireless/wifi interface per heartbeat. Chunk C will add ``client``.

All exceptions from the Influx client are caught internally. Design Section
5.3 is explicit: telemetry ingest (the 204 response path) must not depend on
Influx being up. A flaky or offline Influx must NOT bubble up as a 500 to
the router -- the router would then retry-spam and the liveness timestamp
would stop updating, which is exactly the signal we need to keep working.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from app.config import get_settings
from app.models import Router

log = logging.getLogger(__name__)

# Singleton client. Initialized lazily so the app can boot without Influx
# configured (dev, tests, or a deploy that temporarily lost the token).
_client: InfluxDBClientAsync | None = None
_client_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Uptime parser
# ---------------------------------------------------------------------------

# RouterOS formats uptime like "1w2d3h4m5s" -- any subset of units,
# largest-first, all integer. Examples in the wild:
#   "23m12s", "5s", "1d4h", "2w1d6h3m9s".
_UPTIME_RE = re.compile(
    r"^"
    r"(?:(?P<w>\d+)w)?"
    r"(?:(?P<d>\d+)d)?"
    r"(?:(?P<h>\d+)h)?"
    r"(?:(?P<m>\d+)m)?"
    r"(?:(?P<s>\d+)s)?"
    r"$"
)
_UPTIME_UNIT_SEC = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_uptime(s: str) -> int:
    """Convert RouterOS uptime string to total seconds.

    Returns 0 for empty or unparseable input rather than raising -- a bad
    uptime string should not poison the whole write path, and an empty
    string just means the router didn't populate the field.
    """
    if not s:
        return 0
    m = _UPTIME_RE.match(s.strip())
    if not m or not any(m.groupdict().values()):
        return 0
    total = 0
    for unit, seconds in _UPTIME_UNIT_SEC.items():
        v = m.group(unit)
        if v is not None:
            total += int(v) * seconds
    return total


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

async def get_client() -> InfluxDBClientAsync | None:
    """Lazy-init the singleton ``InfluxDBClientAsync``.

    Returns ``None`` when no token is configured -- the dev/test escape
    hatch. Callers should treat a ``None`` return as "Influx is disabled
    for this process" and skip the write.
    """
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        settings = get_settings()
        if not settings.influx_token:
            log.info("influx: no token configured -- writes disabled")
            return None
        _client = InfluxDBClientAsync(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
        )
        log.info(
            "influx: client initialised url=%s org=%s bucket=%s",
            settings.influx_url,
            settings.influx_org,
            settings.influx_bucket,
        )
        return _client


async def close_client() -> None:
    """Close the singleton client on app shutdown.

    Safe to call when the client was never initialised.
    """
    global _client
    if _client is None:
        return
    try:
        await _client.close()
    except Exception:  # pragma: no cover -- defensive
        log.exception("influx: error closing client")
    _client = None


# ---------------------------------------------------------------------------
# Point builders
# ---------------------------------------------------------------------------

def _build_system_point(
    router: Router,
    uptime_sec: int,
    system: dict[str, Any] | None,
    ts: datetime,
) -> Point:
    """Build a ``system`` measurement point per design Section 5.4.

    Tags: ``{router_id, model, wifi_stack}``.

    Fields:
      * ``uptime_sec`` -- always emitted (0 if uptime couldn't be parsed).
      * ``cpu_load_pct`` (int), ``free_memory_bytes`` (int),
        ``total_memory_bytes`` (int), ``temperature_c`` (float),
        ``voltage_v`` (float) -- emitted only when the router actually sent
        them. Deliverable #8 Chunk A.

    Conditional emission is deliberate: a router without a temperature
    sensor (e.g. hAP ac2) omits the field rather than sending 0, so Grafana
    can distinguish "no sensor" from "sensor reads zero".
    """
    point = (
        Point("system")
        .tag("router_id", str(router.id))
        .tag("model", router.model or "unknown")
        .tag("wifi_stack", router.wifi_stack or "unknown")
        .field("uptime_sec", int(uptime_sec))
    )

    if system:
        for name in ("cpu_load_pct", "free_memory_bytes", "total_memory_bytes"):
            val = system.get(name)
            if val is not None:
                point = point.field(name, int(val))
        for name in ("temperature_c", "voltage_v"):
            val = system.get(name)
            if val is not None:
                point = point.field(name, float(val))

    return point.time(ts)


# ---------------------------------------------------------------------------
# Interface points (Chunk B)
# ---------------------------------------------------------------------------

# Numeric counter fields shared across all three interface kinds. Always
# emitted as ints with the Influx ``i`` suffix so they don't get accidentally
# coerced to floats by a stray RouterOS quirk.
_INTERFACE_COUNTER_FIELDS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")

# Per-kind extra integer fields (wireless has frequency, wifi has none).
# tx_power stays a string field because RouterOS may emit "default".
_WIRELESS_INT_FIELDS = ("frequency",)


def _build_interface_points(
    router: Router,
    payload: dict[str, Any],
    ts: datetime,
) -> list[Point]:
    """Build per-interface points for the ``interface`` measurement.

    Tags: ``{router_id, interface_name, kind}`` where ``kind`` ∈
    ``{ethernet, wireless, wifi}``. Three tags keep cardinality bounded:
    ``router_id`` is the dominant axis, ``interface_name`` is small per
    router (typically ≤6), and ``kind`` is a 3-value enum.

    Fields:
      * Always (per kind): rx_bytes, tx_bytes, rx_packets, tx_packets,
        running (ethernet) / disabled (wireless+wifi) -- all conditionally
        emitted, never as zero placeholders.
      * Wireless only: ssid, band, channel_width, mode, tx_power (string
        fields), frequency (int field).
      * Wifi only: ssid, channel, configuration (string fields).

    Returns an empty list if the payload has no interface arrays. The
    caller batches these alongside the system point so we make one
    write_api call per heartbeat.
    """
    points: list[Point] = []

    eth = payload.get("ethernet")
    if isinstance(eth, list):
        for entry in eth:
            if isinstance(entry, dict):
                p = _build_one_interface_point(router, "ethernet", entry, ts)
                if p is not None:
                    points.append(p)

    wireless = payload.get("wireless_interfaces")
    if isinstance(wireless, list):
        for entry in wireless:
            if isinstance(entry, dict):
                p = _build_one_interface_point(router, "wireless", entry, ts)
                if p is not None:
                    points.append(p)

    wifi = payload.get("wifi_interfaces")
    if isinstance(wifi, list):
        for entry in wifi:
            if isinstance(entry, dict):
                p = _build_one_interface_point(router, "wifi", entry, ts)
                if p is not None:
                    points.append(p)

    return points


def _build_one_interface_point(
    router: Router,
    kind: str,
    entry: dict[str, Any],
    ts: datetime,
) -> Point | None:
    """Build a single ``interface`` point, or ``None`` if it would be empty.

    A point with no fields is invalid in Influx (the line-protocol parser
    rejects ``measurement,tags  <ts>``), so we drop entries that have only
    a name -- they'd carry no information anyway.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        # No name -> no useful identity. Skip rather than emit a point
        # tagged interface_name="" which would alias every nameless entry.
        return None

    point = (
        Point("interface")
        .tag("router_id", str(router.id))
        .tag("interface_name", name)
        .tag("kind", kind)
    )

    has_field = False

    # Counters (all kinds)
    for fname in _INTERFACE_COUNTER_FIELDS:
        val = entry.get(fname)
        if val is not None:
            try:
                point = point.field(fname, int(val))
                has_field = True
            except (TypeError, ValueError):
                # Non-numeric counter slipped through -- skip rather than
                # crash the whole write. Logged at debug to keep noise down.
                log.debug(
                    "influx: dropped non-int counter %s=%r on %s/%s",
                    fname, val, kind, name,
                )

    # Link state -- ethernet uses ``running``, wireless/wifi use ``disabled``.
    # Both are emitted as bool fields so Grafana can stat() them.
    if kind == "ethernet":
        running = entry.get("running")
        if isinstance(running, bool):
            point = point.field("running", running)
            has_field = True
    else:
        disabled = entry.get("disabled")
        if isinstance(disabled, bool):
            point = point.field("disabled", disabled)
            has_field = True

    # Wireless metadata
    if kind == "wireless":
        for fname in _WIRELESS_INT_FIELDS:
            val = entry.get(fname)
            if val is not None:
                try:
                    point = point.field(fname, int(val))
                    has_field = True
                except (TypeError, ValueError):
                    log.debug(
                        "influx: dropped non-int wireless field %s=%r on %s",
                        fname, val, name,
                    )
        for fname in ("ssid", "band", "channel_width", "tx_power", "mode"):
            val = entry.get(fname)
            if isinstance(val, str) and val:
                point = point.field(fname, val)
                has_field = True

    # Wifi (wave2) metadata
    if kind == "wifi":
        for fname in ("ssid", "channel", "configuration"):
            val = entry.get(fname)
            if isinstance(val, str) and val:
                point = point.field(fname, val)
                has_field = True

    if not has_field:
        # Tag-only point would be rejected by Influx.
        return None

    return point.time(ts)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def _extract_uptime_string(payload: dict[str, Any]) -> str:
    """Pick the uptime string to parse.

    Phase 2 puts uptime inside ``system``; Phase 1 had it at top level. We
    prefer nested so re-enrolled routers use the design Section 5.2 shape,
    but fall through to flat so old templates keep working during rolling
    deploys.
    """
    system = payload.get("system")
    if isinstance(system, dict):
        nested = system.get("uptime")
        if nested:
            return str(nested)
    flat = payload.get("uptime")
    return str(flat) if flat else ""


async def write_telemetry(router: Router, payload: dict[str, Any]) -> None:
    """Write a telemetry payload to Influx.

    Chunk B behaviour: build the ``system`` point as before, then append
    one ``interface`` point per ethernet/wireless/wifi entry. All points
    go in a single ``write_api.write(record=[...])`` call so the heartbeat
    either lands atomically or fails as a unit -- we don't want the system
    point to land while the interface points fail and leave a confusing
    half-state in Influx.

    Any exception from the Influx client path is logged and swallowed --
    the 204 response path is authoritative for "router heartbeat received"
    (we've already updated ``last_seen_at`` in Postgres by the time this
    is called).
    """
    client = await get_client()
    if client is None:
        return
    try:
        ts = datetime.now(timezone.utc)
        uptime_sec = _parse_uptime(_extract_uptime_string(payload))
        system_raw = payload.get("system")
        system = system_raw if isinstance(system_raw, dict) else None
        system_point = _build_system_point(router, uptime_sec, system, ts)
        interface_points = _build_interface_points(router, payload, ts)

        records = [system_point, *interface_points]
        settings = get_settings()
        write_api = client.write_api()
        await write_api.write(bucket=settings.influx_bucket, record=records)
        log.debug(
            "influx: wrote router_id=%s uptime_sec=%s system_fields=%s interfaces=%d",
            router.id,
            uptime_sec,
            sorted(system.keys()) if system else [],
            len(interface_points),
        )
    except Exception:  # pragma: no cover -- defensive, all errors logged
        log.warning(
            "influx: write failed router_id=%s -- 204 still returned to router",
            router.id,
            exc_info=True,
        )
