"""WireGuard config synchronization + overlay IP allocation.

Two jobs live here:

* ``sync_from_db`` - render ``/etc/wireguard/wg0.conf`` from the Postgres
  ``routers`` table and reload WireGuard in-place (no tunnel drops).
* ``allocate_overlay_ip`` - pick the lowest unused host IP in the overlay
  network for a new router. Called during enrollment.

Both use the overlay network from ``settings.wg_overlay_cidr``
(``10.100.0.0/22`` by default - gives us ~1022 usable hosts).

Security posture:
  * The FastAPI process runs as the unprivileged ``cpecloud`` user.
  * It has write access to ``/etc/wireguard/wg0.conf`` (via the
    ``root:cpecloud`` ``640`` ownership set up in INSTALL.md section 9).
  * It has NO general sudo. The *only* thing it can sudo is
    ``/usr/local/sbin/cpe-cloud-wg-sync``, which runs ``wg syncconf`` on wg0.
  * It never reads the server's WG private key - that lives in the
    ``[Interface]`` stanza of the existing ``wg0.conf``, which we copy
    through verbatim.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from ipaddress import IPv4Address, IPv4Network, ip_address
from pathlib import Path
from typing import Iterable, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Router

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types + templates
# ---------------------------------------------------------------------------

class PeerRow(NamedTuple):
    identity: str
    public_key: str
    overlay_ip: str  # dotted-decimal, no prefix

    def to_stanza(self) -> str:
        # PersistentKeepalive is critical for CPE behind NAT: without it, the
        # carrier-grade NAT drops the UDP flow after ~30-60s of idle and the
        # server can't initiate.
        return (
            f"# {self.identity}\n"
            f"[Peer]\n"
            f"PublicKey = {self.public_key}\n"
            f"AllowedIPs = {self.overlay_ip}/32\n"
            f"PersistentKeepalive = 25\n"
        )


MANAGED_MARKER = "# --- managed peers (do not edit below; regenerated from Postgres) ---"


# ---------------------------------------------------------------------------
# Pure helpers - no DB, easy to unit-test
# ---------------------------------------------------------------------------

def _read_interface_block(path: Path) -> str:
    """Return the [Interface] stanza from wg0.conf, dropping existing peers.

    The split point is the first [Peer] header OR the managed marker,
    whichever comes first. Anything above is preserved verbatim (including
    comments and blank lines - so manual [Interface] edits like a custom
    ListenPort stick).
    """
    if not path.exists():
        raise RuntimeError(
            f"Missing {path} - did the wg-quick bootstrap (INSTALL.md section 5) run?"
        )
    content = path.read_text()

    # Find the earliest of: managed marker, first [Peer] header.
    candidates = [content.find(marker) for marker in (MANAGED_MARKER, "[Peer]")]
    candidates = [c for c in candidates if c != -1]
    if not candidates:
        return content.rstrip() + "\n"
    cut = min(candidates)
    return content[:cut].rstrip() + "\n"


def _render_config(interface_block: str, peers: Iterable[PeerRow]) -> str:
    """Assemble the final wg0.conf text."""
    parts = [interface_block.rstrip(), "", MANAGED_MARKER, ""]
    for peer in peers:
        parts.append(peer.to_stanza())
    return "\n".join(parts).rstrip() + "\n"


def _write_atomic(path: Path, content: str) -> None:
    """Write via temp file + atomic rename. Preserves owner + mode."""
    stat = path.stat()
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".wg0.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w") as tf:
            tf.write(content)
        # Match the original file's owner + mode so post-rename permissions
        # don't regress.
        os.chmod(tmp_path, stat.st_mode & 0o7777)
        try:
            os.chown(tmp_path, stat.st_uid, stat.st_gid)
        except PermissionError:
            # On non-root runs (tests, dev) chown will fail - that's OK,
            # mode is the important part for correctness.
            log.debug("skipping chown on %s - insufficient privileges", tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# DB-touching helpers
# ---------------------------------------------------------------------------

def _coerce_ip(value) -> IPv4Address:
    """Normalize whatever psycopg3 returns for INET into an IPv4Address."""
    if isinstance(value, IPv4Address):
        return value
    # psycopg can return ipaddress.IPv4Interface or a plain string like
    # '10.100.0.2/32'. Strip any prefix before parsing.
    s = str(value).split("/", 1)[0]
    addr = ip_address(s)
    if not isinstance(addr, IPv4Address):
        raise ValueError(f"Expected IPv4 address in overlay network, got {value!r}")
    return addr


async def _fetch_active_peers(session: AsyncSession) -> list[PeerRow]:
    stmt = (
        select(Router.identity, Router.wg_public_key, Router.wg_overlay_ip)
        .where(Router.status == "active")
        .where(Router.wg_public_key.is_not(None))
        .where(Router.wg_overlay_ip.is_not(None))
        .order_by(Router.wg_overlay_ip)
    )
    rows = (await session.execute(stmt)).all()
    peers: list[PeerRow] = []
    for identity, public_key, overlay_ip in rows:
        peers.append(
            PeerRow(
                identity=identity,
                public_key=public_key,
                overlay_ip=str(_coerce_ip(overlay_ip)),
            )
        )
    return peers


async def allocate_overlay_ip(session: AsyncSession) -> IPv4Address:
    """Return the lowest unused host IP in the overlay network.

    Concurrency: we rely on Postgres's UNIQUE constraint on
    ``routers.wg_overlay_ip`` to catch races. The caller (enrollment handler)
    must be prepared to retry once on IntegrityError - the probability of a
    collision given a sparse overlay is microscopic, so the retry loop lives
    there rather than here.
    """
    settings = get_settings()
    network: IPv4Network = settings.overlay_network
    server_ip = IPv4Address(settings.wg_server_ip)

    stmt = select(Router.wg_overlay_ip).where(Router.wg_overlay_ip.is_not(None))
    used: set[IPv4Address] = set()
    for (raw,) in (await session.execute(stmt)).all():
        if raw is None:
            continue
        used.add(_coerce_ip(raw))
    used.add(server_ip)

    for candidate in network.hosts():
        if candidate in used:
            continue
        log.debug("overlay ip allocated: %s (used=%d)", candidate, len(used))
        return candidate

    raise RuntimeError(
        f"Overlay network {network} is exhausted - all "
        f"{network.num_addresses - 2} host IPs in use. Expand WG_OVERLAY_CIDR."
    )


# ---------------------------------------------------------------------------
# Helper subprocess
# ---------------------------------------------------------------------------

async def _run_sync_helper() -> None:
    """Exec the sudo-wrapped helper that runs ``wg syncconf wg0``."""
    settings = get_settings()
    helper = str(settings.wg_sync_helper)
    log.info("running wg sync helper: sudo -n %s", helper)
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        helper,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"wg sync helper failed (rc={proc.returncode}): "
            f"stderr={stderr.decode(errors='replace').strip()!r}"
        )
    if stdout:
        log.debug("wg sync stdout: %s", stdout.decode(errors="replace").strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def sync_from_db(session: AsyncSession) -> dict:
    """Regenerate wg0.conf from Postgres and reload WireGuard in-place.

    Safe to call redundantly - the rendered config is deterministic for a
    given DB state, so duplicate calls are a no-op at the kernel level.

    Returns a small summary dict suitable for logging / audit.
    """
    settings = get_settings()
    path: Path = settings.wg_config_path

    peers = await _fetch_active_peers(session)
    interface_block = await asyncio.to_thread(_read_interface_block, path)
    rendered = _render_config(interface_block, peers)

    await asyncio.to_thread(_write_atomic, path, rendered)
    await _run_sync_helper()

    log.info("wireguard sync complete: %d peer(s)", len(peers))
    return {
        "peers_synced": len(peers),
        "config_path": str(path),
    }
