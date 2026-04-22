"""InfluxDB writer — thin wrapper over influxdb-client.

Stub. Real implementation takes a normalized telemetry payload and writes
the `system`, `interface`, and `client` measurements with router_id + identity
as tags. See `01-design-wireguard-and-telemetry.md` §5.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def write_telemetry(router_id: int, payload: dict) -> None:
    """Not yet implemented."""
    log.debug("influx.write_telemetry: stub — router_id=%s", router_id)
