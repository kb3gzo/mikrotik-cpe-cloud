"""InfluxDB writer for telemetry heartbeats.

Design Section 5.4 defines three measurements -- ``system``, ``interface``,
and ``client``. Phase 1 (Task #22) only writes ``system`` with
``uptime_sec``, because that's all the current ``telemetry-*.rsc.j2``
templates send. Deliverable #8 expands the RouterOS scrape to include full
interface and client registration data, at which point this module grows
``interface`` and ``client`` point builders.

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

    Phase 2 (Chunk A) behaviour: parse uptime (from ``system.uptime`` if
    present, else flat ``uptime``), then build a single ``system`` point
    with whatever optional fields the router included. Any exception from
    the Influx client path is logged and swallowed -- the 204 response path
    is authoritative for "router heartbeat received" (we've already updated
    ``last_seen_at`` in Postgres by the time this is called).
    """
    client = await get_client()
    if client is None:
        return
    try:
        ts = datetime.now(timezone.utc)
        uptime_sec = _parse_uptime(_extract_uptime_string(payload))
        system_raw = payload.get("system")
        system = system_raw if isinstance(system_raw, dict) else None
        point = _build_system_point(router, uptime_sec, system, ts)
        settings = get_settings()
        write_api = client.write_api()
        await write_api.write(bucket=settings.influx_bucket, record=point)
        log.debug(
            "influx: wrote system point router_id=%s uptime_sec=%s fields=%s",
            router.id,
            uptime_sec,
            sorted(system.keys()) if system else [],
        )
    except Exception:  # pragma: no cover -- defensive, all errors logged
        log.warning(
            "influx: write failed router_id=%s -- 204 still returned to router",
            router.id,
            exc_info=True,
        )
