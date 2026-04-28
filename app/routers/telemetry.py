"""Telemetry push endpoint -- routers POST a heartbeat every 5 min.

Phase 1 ingest (Task #20/#22): authenticate the bearer token, update
``Router.last_seen_at``, call the Influx writer, return 204.

Deliverable #8 Chunk A expanded the accepted payload with top-level device
identity + a nested ``system`` block. Chunk B adds three optional
per-interface arrays (``ethernet``, ``wireless_interfaces``,
``wifi_interfaces``). Chunk C will add ``clients``. Every Chunk B field is
Optional, so a Phase 1 / Chunk-A-only payload still validates -- this is
the rolling-deploy invariant: server can ship ahead of routers.

Request shape (from ``telemetry-wireless.rsc.j2`` / ``telemetry-wifi.rsc.j2``):

    POST /api/v1/telemetry
    Authorization: Bearer <raw telemetry token>
    Content-Type: application/json

    {"schema_version": 1,
     "identity": "hAP ac2 - Lab, Bench",
     "serial": "HC12345678", "mac": "E4:8D:8C:AA:BB:CC",
     "board": "RB952Ui-5ac2nD", "ros_version": "7.14.2",
     "wifi_stack": "wireless",
     "system": {"uptime": "5m23s", "cpu_load_pct": 4,
                "free_memory_bytes": 56213504,
                "total_memory_bytes": 134217728}}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Router
from app.services.influx import write_telemetry
from app.services.rate_limit import check_rate_limit
from app.services.tokens import find_valid_router_token, prefix_of

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["telemetry"])


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class TelemetrySystem(BaseModel):
    """Nested ``system`` metrics per design Section 5.2 / Section 5.4.

    All fields optional: a router that doesn't expose ``/system health``
    entries (e.g. hAP ac2 has no temperature probe) simply omits those
    fields rather than sending zero. The point builder mirrors this -- a
    missing field is not written, so Influx doesn't accumulate nonsense
    zeroes.
    """

    model_config = ConfigDict(extra="allow")

    uptime: str | None = Field(default=None, min_length=1, max_length=32)
    cpu_load_pct: int | None = Field(default=None, ge=0, le=100)
    free_memory_bytes: int | None = Field(default=None, ge=0)
    total_memory_bytes: int | None = Field(default=None, ge=0)
    # Wide bounds -- Mikrotik returns integer Celsius, but allow float for
    # future hardware. -20..120 catches real sensor readings while rejecting
    # unit-confusion bugs (e.g. raw ADC values in the thousands).
    temperature_c: float | None = Field(default=None, ge=-20, le=120)
    voltage_v: float | None = Field(default=None, ge=0, le=60)


# ---------------------------------------------------------------------------
# Chunk B: per-interface payload entries (design Section 5.2)
# ---------------------------------------------------------------------------
#
# All three interface models below carry the same byte/packet counters --
# they're the meat of Chunk B. The wireless vs wifi split exists because the
# RouterOS command trees expose different metadata (band/frequency/mode in
# ``/interface wireless`` vs channel/configuration in ``/interface wifi``).
# Neither stack populates errors/drops in the templates yet; if we add them
# later they'll slot in via ``extra="allow"`` first, then get promoted to
# real fields once the wire format is stable.
#
# Numeric counters are typed ``int | None`` and Pydantic coerces strings to
# ints (default lax mode) -- some RouterOS firmwares quote large counters.

class TelemetryEthernet(BaseModel):
    """One ethernet interface entry.

    RouterOS exposes the same shape on both stacks (classic and wave2),
    so this model is shared. ``running`` is True when link is up.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=32)
    running: bool | None = None
    rx_bytes: int | None = Field(default=None, ge=0)
    tx_bytes: int | None = Field(default=None, ge=0)
    rx_packets: int | None = Field(default=None, ge=0)
    tx_packets: int | None = Field(default=None, ge=0)


class TelemetryWirelessIf(BaseModel):
    """Classic ``/interface wireless`` entry (hAP ac/ac2/ac3, lite, lhg).

    String metadata fields stay strings -- ``tx_power`` for instance can be
    either a numeric dBm value or the literal ``"default"`` depending on
    config, and we'd rather preserve that ambiguity than coerce.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=32)
    ssid: str | None = Field(default=None, max_length=64)
    band: str | None = Field(default=None, max_length=32)
    frequency: int | None = Field(default=None, ge=0, le=8000)
    channel_width: str | None = Field(default=None, max_length=32)
    tx_power: str | None = Field(default=None, max_length=16)
    disabled: bool | None = None
    mode: str | None = Field(default=None, max_length=32)
    rx_bytes: int | None = Field(default=None, ge=0)
    tx_bytes: int | None = Field(default=None, ge=0)
    rx_packets: int | None = Field(default=None, ge=0)
    tx_packets: int | None = Field(default=None, ge=0)


class TelemetryWifiIf(BaseModel):
    """Wave2 ``/interface wifi`` entry (hAP ax2/ax3 on ROS 7.13+).

    The wave2 stack hides the raw frequency behind ``channel`` (a string
    like ``"5180/20mhz"``); we stash the raw string and parse server-side
    if/when we want to graph frequency directly.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=32)
    ssid: str | None = Field(default=None, max_length=64)
    channel: str | None = Field(default=None, max_length=32)
    disabled: bool | None = None
    configuration: str | None = Field(default=None, max_length=64)
    rx_bytes: int | None = Field(default=None, ge=0)
    tx_bytes: int | None = Field(default=None, ge=0)
    rx_packets: int | None = Field(default=None, ge=0)
    tx_packets: int | None = Field(default=None, ge=0)


class TelemetryHeartbeat(BaseModel):
    """Telemetry push payload.

    Shape evolution:
      * Phase 1 (Task #20): flat ``{identity, uptime, wifi_stack}``.
      * Phase 2 / Deliverable #8 Chunk A: adds top-level device identity
        (``serial``, ``mac``, ``board``, ``ros_version``) and a nested
        ``system`` block with CPU/memory/temperature.

    Both shapes are accepted during a rolling deploy -- fields added in
    Chunk A are all optional, and the old flat ``uptime`` is still honoured
    when ``system`` is absent. ``extra="allow"`` keeps the door open for
    Chunks B (interface) and C (clients) to ship without another schema bump.
    """

    model_config = ConfigDict(extra="allow")

    # Envelope
    schema_version: int | None = Field(default=None, ge=1, le=100)

    # Stable device identity
    identity: str = Field(min_length=1, max_length=128)
    wifi_stack: str = Field(pattern=r"^(wireless|wifi)$")
    serial: str | None = Field(default=None, min_length=4, max_length=64)
    # MAC from ``/interface ethernet get ether1 mac-address`` -- uppercase,
    # colon-separated. 17 chars exact (XX:XX:XX:XX:XX:XX).
    mac: str | None = Field(
        default=None,
        pattern=r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$",
    )
    board: str | None = Field(default=None, min_length=1, max_length=64)
    ros_version: str | None = Field(default=None, min_length=1, max_length=32)

    # Legacy flat uptime -- still accepted; superseded by system.uptime when
    # both are present.
    uptime: str | None = Field(default=None, min_length=1, max_length=32)

    # Nested metrics
    system: TelemetrySystem | None = None

    # Chunk B: per-interface arrays. All three are optional so a Phase 1 or
    # Chunk-A-only payload still validates. ``ethernet`` is shared across
    # stacks; ``wireless_interfaces`` is classic-stack only and
    # ``wifi_interfaces`` is wave2-stack only -- a router populates exactly
    # one of those two based on its ``wifi_stack`` value, never both.
    ethernet: list[TelemetryEthernet] | None = None
    wireless_interfaces: list[TelemetryWirelessIf] | None = None
    wifi_interfaces: list[TelemetryWifiIf] | None = None


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _extract_bearer(authorization: str | None) -> str:
    """Extract the raw token from an ``Authorization: Bearer <token>`` header.

    Raises 401 if missing or malformed.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed Authorization header, expected 'Bearer <token>'",
        )
    return parts[1].strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/telemetry",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def push_telemetry(
    request: Request,
    payload: TelemetryHeartbeat,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    source_ip = request.client.host if request.client else "unknown"
    raw = _extract_bearer(authorization)

    # --- Pre-auth IP rate-limit to blunt enumeration ------------------------
    if not await check_rate_limit("telemetry-ip", source_ip):
        log.warning("telemetry rate limited (ip) ip=%s", source_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
        )

    # --- Token lookup -------------------------------------------------------
    token_row = await find_valid_router_token(session, raw)
    if token_row is None:
        log.warning(
            "telemetry bad token ip=%s prefix=%s",
            source_ip, prefix_of(raw),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked telemetry token",
        )

    # --- Per-router rate-limit ---------------------------------------------
    if not await check_rate_limit("telemetry-router", str(token_row.router_id)):
        log.warning(
            "telemetry rate limited (router) router_id=%s ip=%s",
            token_row.router_id, source_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate limit exceeded",
        )

    # --- Router state gate --------------------------------------------------
    router_row = await session.get(Router, token_row.router_id)
    if router_row is None:
        # Token row exists but router doesn't -- shouldn't happen (FK w/
        # cascade), but guard explicitly so we fail loud if it ever does.
        log.error(
            "telemetry orphan token token_id=%s router_id=%s",
            token_row.id, token_row.router_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked telemetry token",
        )
    if router_row.status in ("decommissioned", "quarantined"):
        log.warning(
            "telemetry from %s router router_id=%s identity=%r",
            router_row.status, router_row.id, router_row.identity,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"router is {router_row.status}",
        )

    # --- Update liveness ----------------------------------------------------
    router_row.last_seen_at = datetime.now(timezone.utc)
    await session.commit()

    # --- Influx write (errors logged internally) ----------------------------
    # write_telemetry catches its own exceptions -- an unreachable Influx
    # must not break the 204 response path (design Section 5.3). The outer
    # try/except is defense-in-depth: if a future refactor ever lets an
    # exception escape write_telemetry, this handler still returns 204 so
    # the router's liveness timestamp keeps advancing.
    try:
        await write_telemetry(router_row, payload.model_dump())
    except Exception:  # pragma: no cover -- defended by inner catch too
        log.warning(
            "telemetry: write_telemetry raised for router_id=%s -- 204 still returned",
            router_row.id,
            exc_info=True,
        )

    log.debug(
        "telemetry ok router_id=%s identity=%r uptime=%s wifi_stack=%s",
        router_row.id,
        payload.identity,
        payload.uptime,
        payload.wifi_stack,
    )
    # Bare Response with 204 -- avoids FastAPI trying to serialize a return
    # value into a response model on a no-body endpoint.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
