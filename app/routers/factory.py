"""Factory pre-provisioning installer endpoint.

Implements `GET /factory/self-enroll.rsc` per `02-self-provisioning.md` section 3.2.

The factory-prep script (run once per shelf-stock hAP during initial prep)
downloads this rendered installer, imports it, and is done. The returned
installer embeds:

* The current plaintext provisioning secret (read from settings at request
  time, NOT from Postgres - Postgres only stores hashes).
* The enrollment URL + server FQDN the router should contact on first boot.

Auth: short-lived `AdminFetchToken`, passed as `?t=<token>`. RouterOS's
`/tool fetch` can set custom headers but the query-param form is what the
spec calls for and it is less error-prone for the operator pasting the token
into a prep script.

Errors return RSC scripts (text/plain) rather than JSON - that way the
operator sees a readable `/log error` entry on the router instead of cryptic
fetch errors. HTTP status codes are still set appropriately for server-side
monitoring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response, status
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.services.rate_limit import check_fetch_rate_limit
from app.services.tokens import find_valid_admin_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/factory", tags=["factory"])


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "rsc"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),
    keep_trailing_newline=True,
)


def _rsc_error(message: str) -> str:
    """Format an error payload as a tiny RouterOS script."""
    safe = message.replace('"', "'")
    return f':log error "cpe-cloud factory-prep rejected: {safe}"\n'


def _rsc_response(body: str, status_code: int = 200) -> Response:
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/plain; charset=utf-8",
    )


@router.get("/self-enroll.rsc", response_class=Response)
async def factory_installer(
    request: Request,
    t: str = Query(
        ...,
        min_length=16,
        max_length=128,
        description="Admin fetch token (raw) minted via `python -m app.cli fetch-tokens mint`.",
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source_ip = request.client.host if request.client else "unknown"

    if not await check_fetch_rate_limit(source_ip):
        log.warning("factory-prep rate limited from %s", source_ip)
        return _rsc_response(
            _rsc_error("rate limit exceeded"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    token_row = await find_valid_admin_token(session, t)
    if token_row is None:
        log.warning("factory-prep bad fetch token from %s", source_ip)
        return _rsc_response(
            _rsc_error("fetch token expired or invalid"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    settings = get_settings()
    secret = settings.provisioning_secret_current
    if not secret:
        log.error("factory-prep: no current provisioning secret configured")
        return _rsc_response(
            _rsc_error("server has no active provisioning secret"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    token_row.last_used_at = datetime.now(timezone.utc)
    token_row.use_count += 1
    await session.commit()

    template = _jinja_env.get_template("factory-installer.rsc.j2")
    rsc = template.render(
        provisioning_secret=secret,
        enrollment_url=settings.enrollment_url,
        server_fqdn=settings.server_fqdn,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        token_label=token_row.label,
    )
    log.info(
        "factory-prep served to %s (token=%s label=%r use=%d)",
        source_ip,
        token_row.token_prefix,
        token_row.label,
        token_row.use_count,
    )
    return _rsc_response(rsc)
