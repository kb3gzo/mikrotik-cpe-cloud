"""Zero-touch auto-enrollment endpoint.

Implements ``POST /api/v1/auto-enroll`` per ``02-self-provisioning.md`` s.4.

Called by the on-device ``cpe-cloud-enroll`` script after a router has booted,
obtained DHCP, resolved the server FQDN, and generated its WireGuard keypair.
The handler:

1. Rate-limits by source IP and by serial (10/min each).
2. Validates ``X-Provisioning-Secret`` (constant-time, current OR previous).
3. Inserts a new ``Router`` row (or updates the existing one if this is a
   re-enrollment after factory reset - keeps the overlay IP).
4. Mints a telemetry token - raw value returned to the router exactly once,
   only the hash+prefix is persisted.
5. Regenerates ``wg0.conf`` and triggers ``wg syncconf``.
6. Renders ``provision.rsc.j2`` and returns it as ``text/plain``.

Errors return RSC scripts (a ``:log error`` line) rather than JSON so the
operator sees a readable message in ``/log`` on the router. HTTP status codes
are still set correctly (401/429/500) for server-side monitoring.

Phase 1 scope note:
  Provisioning-secret verification uses ``Settings.provisioning_secret_current``
  / ``provisioning_secret_previous`` directly - NOT the ``provisioning_secrets``
  Postgres table described in ``02-self-provisioning.md`` s.2.2. When
  Deliverable #7 lands the ``provisioning-secret`` CLI, we migrate this to
  query the DB-backed hash table. Until then, env-var secrets are authoritative
  for both installer render (factory.py) AND verify (here).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request, Response, status
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import AuditLog, Router, RouterToken
from app.services import wireguard as wg_service
from app.services.rate_limit import check_rate_limit
from app.services.tokens import (
    hash_token,
    mint_telemetry_token,
    prefix_of,
    verify_token,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["auto-enroll"])


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "rsc"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),
    keep_trailing_newline=True,
)


# Auto-approve regex: identity matching the Bradford naming convention gets
# status='active' on first enrollment. Anything else lands in 'pending' and
# waits for admin review. See spec s.2.3.
_IDENTITY_AUTO_APPROVE = re.compile(r"^hAP .+ - .+, .+$")


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class AutoEnrollRequest(BaseModel):
    """Payload POSTed by the on-device cpe-cloud-enroll script."""

    serial: str = Field(min_length=4, max_length=64)
    mac: str = Field(min_length=17, max_length=17)
    model: str = Field(min_length=1, max_length=64)
    identity: str = Field(min_length=1, max_length=128)
    ros_version: str = Field(min_length=1, max_length=32)
    wifi_stack: str = Field(pattern=r"^(wireless|wifi)$")
    # WireGuard public keys are 32 bytes base64 = 44 chars (43 base + '=').
    router_public_key: str = Field(min_length=43, max_length=44)


# ---------------------------------------------------------------------------
# RSC helpers
# ---------------------------------------------------------------------------

def _rsc_error(message: str) -> str:
    safe = message.replace('"', "'")
    return f':log error "cpe-cloud enrollment rejected: {safe}"\n'


def _rsc_response(body: str, status_code: int = 200) -> Response:
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/plain; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Secret verification (env-based; see module docstring)
# ---------------------------------------------------------------------------

def _verify_provisioning_secret(raw: str, settings: Settings) -> str | None:
    """Return the matched secret slot ('current'/'previous') or None.

    Constant-time comparisons against BOTH slots - we always evaluate both
    to avoid timing oracle leakage on which slot matched.
    """
    current = settings.provisioning_secret_current
    previous = settings.provisioning_secret_previous

    # Precompute expected hashes (empty strings produce a stable hash that
    # will never match a non-empty raw input, keeping the compare paths
    # uniform regardless of which slots are populated).
    expected_current = hash_token(current) if current else None
    expected_previous = hash_token(previous) if previous else None

    matched: str | None = None
    if expected_current is not None and verify_token(raw, expected_current):
        matched = "current"
    if expected_previous is not None and verify_token(raw, expected_previous):
        # Do not short-circuit even if `matched` is already set - constant-
        # time eval of both slots.
        if matched is None:
            matched = "previous"
    return matched


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _parse_ros_major(version: str) -> int | None:
    try:
        return int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        return None


def _initial_status_for(identity: str) -> str:
    """Phase 1 auto-approve rule (spec s.2.3).

    Phase 2 will replace this with lookups against the ``provisioning_rules``
    table + admin-curated pre-registered serial list.
    """
    if _IDENTITY_AUTO_APPROVE.match(identity):
        return "active"
    return "pending"


async def _audit(
    session: AsyncSession,
    *,
    action: str,
    status_: str,
    router_id: int | None,
    params: dict | None,
) -> None:
    session.add(
        AuditLog(
            actor="auto-enroll",
            action=action,
            status=status_,
            router_id=router_id,
            params=params or {},
        )
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/auto-enroll",
    response_class=Response,
    responses={
        200: {"content": {"text/plain": {}}},
        401: {"content": {"text/plain": {}}},
        429: {"content": {"text/plain": {}}},
        500: {"content": {"text/plain": {}}},
    },
)
async def auto_enroll(
    request: Request,
    payload: AutoEnrollRequest,
    x_provisioning_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source_ip = request.client.host if request.client else "unknown"
    settings = get_settings()

    # --- Rate limiting ------------------------------------------------------
    ip_ok = await check_rate_limit("auto-enroll-ip", source_ip)
    serial_ok = await check_rate_limit("auto-enroll-serial", payload.serial)
    if not (ip_ok and serial_ok):
        log.warning(
            "auto-enroll rate limited ip=%s serial=%s",
            source_ip, payload.serial,
        )
        return _rsc_response(
            _rsc_error("rate limit exceeded, try again later"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # --- Provisioning secret validation ------------------------------------
    if not x_provisioning_secret:
        log.warning(
            "auto-enroll missing secret ip=%s serial=%s",
            source_ip, payload.serial,
        )
        await _audit(
            session, action="reject_missing_secret", status_="failed",
            router_id=None,
            params={"source_ip": source_ip, "serial": payload.serial},
        )
        await session.commit()
        return _rsc_response(
            _rsc_error("missing provisioning secret header"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    matched_slot = _verify_provisioning_secret(x_provisioning_secret, settings)
    if matched_slot is None:
        log.warning(
            "auto-enroll bad secret ip=%s serial=%s identity=%r",
            source_ip, payload.serial, payload.identity,
        )
        await _audit(
            session, action="reject_bad_secret", status_="failed",
            router_id=None,
            params={
                "source_ip": source_ip,
                "serial": payload.serial,
                "identity": payload.identity,
            },
        )
        await session.commit()
        return _rsc_response(
            _rsc_error("invalid provisioning secret"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # --- Server pubkey available? ------------------------------------------
    try:
        server_pubkey = wg_service.get_server_public_key()
    except RuntimeError as exc:
        log.error("auto-enroll cannot render provision: %s", exc)
        return _rsc_response(
            _rsc_error("server misconfiguration, contact admin"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # --- Find-or-update router row -----------------------------------------
    existing = await session.scalar(
        select(Router).where(Router.serial_number == payload.serial)
    )
    is_reenroll = existing is not None

    if existing is None:
        overlay_ip = await wg_service.allocate_overlay_ip(session)
        router_row = Router(
            identity=payload.identity,
            serial_number=payload.serial,
            mac_address=payload.mac,
            model=payload.model,
            ros_version=payload.ros_version,
            ros_major=_parse_ros_major(payload.ros_version),
            wifi_stack=payload.wifi_stack,
            wg_public_key=payload.router_public_key,
            wg_overlay_ip=overlay_ip,
            enrolled_at=datetime.now(timezone.utc),
            status=_initial_status_for(payload.identity),
        )
        session.add(router_row)
        await session.flush()  # populate .id
        action = "auto_enroll_new"
    else:
        # Re-enrollment: factory reset or key rotation. Keep the overlay IP
        # (so admin bookmarks + monitoring alerts stay stable); rotate pubkey
        # and revoke old telemetry tokens.
        existing.identity = payload.identity
        existing.mac_address = payload.mac
        existing.model = payload.model
        existing.ros_version = payload.ros_version
        existing.ros_major = _parse_ros_major(payload.ros_version)
        existing.wifi_stack = payload.wifi_stack
        existing.wg_public_key = payload.router_public_key
        existing.enrolled_at = datetime.now(timezone.utc)

        # Revoke any still-active tokens for this router
        old_tokens = await session.scalars(
            select(RouterToken).where(
                RouterToken.router_id == existing.id,
                RouterToken.revoked_at.is_(None),
            )
        )
        now = datetime.now(timezone.utc)
        for tok in old_tokens:
            tok.revoked_at = now

        router_row = existing
        action = "auto_enroll_reenroll"

    # --- Mint fresh telemetry token ----------------------------------------
    raw_telemetry_token = mint_telemetry_token()
    session.add(
        RouterToken(
            router_id=router_row.id,
            token_hash=hash_token(raw_telemetry_token),
            token_prefix=prefix_of(raw_telemetry_token),
        )
    )

    # Commit the Router + RouterToken changes BEFORE syncing wg0 so the
    # renderer in wireguard.sync_from_db sees our peer in the DB snapshot.
    await session.commit()

    # --- Regenerate wg0.conf + reload --------------------------------------
    try:
        wg_result = await wg_service.sync_from_db(session)
    except Exception as exc:  # pragma: no cover - integration failure path
        log.exception("auto-enroll wg sync failed: %s", exc)
        await _audit(
            session, action="wg_sync_failed", status_="failed",
            router_id=router_row.id,
            params={"error": str(exc)},
        )
        await session.commit()
        return _rsc_response(
            _rsc_error("server could not update overlay, retry in a few minutes"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    await _audit(
        session,
        action=action,
        status_="success",
        router_id=router_row.id,
        params={
            "source_ip": source_ip,
            "serial": payload.serial,
            "identity": payload.identity,
            "status": router_row.status,
            "matched_secret_slot": matched_slot,
            "wg_peers_synced": wg_result.get("peers_synced"),
            "reenroll": is_reenroll,
        },
    )
    await session.commit()

    log.info(
        "auto-enroll %s router_id=%d identity=%r status=%s ip=%s overlay=%s",
        action,
        router_row.id,
        router_row.identity,
        router_row.status,
        source_ip,
        router_row.wg_overlay_ip,
    )

    # --- Render provisioning RSC -------------------------------------------
    template = _jinja_env.get_template("provision.rsc.j2")
    rsc = template.render(
        router=router_row,
        telemetry_token=raw_telemetry_token,
        server_public_key=server_pubkey,
        server_endpoint=settings.server_fqdn,
        server_port=51820,
        overlay_cidr=settings.wg_overlay_cidr,
        wg_server_ip=settings.wg_server_ip,
        telemetry_url=f"https://{settings.server_fqdn}/api/v1/telemetry",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return _rsc_response(rsc)
