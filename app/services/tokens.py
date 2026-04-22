"""Token primitives + AdminFetchToken DB operations.

Two layers live here:

1. **Primitives** (`mint_token`, `hash_token`, `prefix_of`, `verify_token`) -
   used by both router telemetry tokens and admin fetch tokens. Pure funcs,
   no DB.

2. **AdminFetchToken ops** (`issue_admin_fetch_token`, `find_valid_admin_token`,
   `revoke_admin_fetch_token`, `list_admin_fetch_tokens`) - DB-touching helpers
   used by `app/routers/factory.py` and `app/cli.py`.

Scheme:
  * Raw token is 32 URL-safe bytes (~43 base64url chars)
  * We store sha256(raw) as `token_hash`
  * First 8 chars of the raw token are stored as `token_prefix` for fast
    index lookup before the constant-time hash compare

Argon2 would be overkill - these tokens are long random strings, so sha256
is enough to make reversing them computationally pointless.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminFetchToken

TOKEN_BYTES = 32
PREFIX_LEN = 8


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def mint_token() -> str:
    """Return a fresh URL-safe token string."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prefix_of(raw: str) -> str:
    return raw[:PREFIX_LEN]


def verify_token(raw: str, expected_hash: str) -> bool:
    """Constant-time comparison of a candidate token against a stored hash."""
    return secrets.compare_digest(hash_token(raw), expected_hash)


# ---------------------------------------------------------------------------
# AdminFetchToken DB operations
# ---------------------------------------------------------------------------

class MintedFetchToken(NamedTuple):
    """Result of minting - the only time the raw token ever exists outside the
    operator's clipboard. Callers MUST display `raw` exactly once and never
    persist it."""

    raw: str
    row: AdminFetchToken


async def issue_admin_fetch_token(
    session: AsyncSession,
    *,
    label: str,
    ttl_hours: int,
    issued_by: str | None = None,
) -> MintedFetchToken:
    """Mint a new AdminFetchToken row and return the raw token once.

    The raw token is NOT stored - only its sha256 hash + 8-char prefix.
    Caller is responsible for committing the session.
    """
    if ttl_hours <= 0:
        raise ValueError("ttl_hours must be positive")
    if not label or not label.strip():
        raise ValueError("label is required")

    raw = mint_token()
    row = AdminFetchToken(
        label=label.strip(),
        token_hash=hash_token(raw),
        token_prefix=prefix_of(raw),
        issued_by=issued_by,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )
    session.add(row)
    await session.flush()  # populate row.id
    return MintedFetchToken(raw=raw, row=row)


async def find_valid_admin_token(
    session: AsyncSession, raw: str
) -> AdminFetchToken | None:
    """Return the AdminFetchToken row matching `raw` if it is valid (not
    expired, not revoked). Returns None if no match.

    Uses the token_prefix index to narrow candidates before a constant-time
    hash comparison - so we do not leak timing info about which labels exist.
    """
    now = datetime.now(timezone.utc)
    stmt = select(AdminFetchToken).where(
        AdminFetchToken.token_prefix == prefix_of(raw),
        AdminFetchToken.revoked_at.is_(None),
        AdminFetchToken.expires_at > now,
    )
    expected_hash = hash_token(raw)
    for row in (await session.scalars(stmt)).all():
        # Prefix collisions are theoretically possible (1 in 64^8 per candidate)
        # - iterate and use constant-time compare anyway.
        if secrets.compare_digest(row.token_hash, expected_hash):
            return row
    return None


async def revoke_admin_fetch_token(session: AsyncSession, ident: str) -> int:
    """Revoke all active tokens matching `ident` (label OR token_prefix).

    Returns the count of tokens revoked. Caller commits.
    """
    now = datetime.now(timezone.utc)
    stmt = select(AdminFetchToken).where(
        AdminFetchToken.revoked_at.is_(None),
        (AdminFetchToken.label == ident)
        | (AdminFetchToken.token_prefix == ident),
    )
    rows = (await session.scalars(stmt)).all()
    for row in rows:
        row.revoked_at = now
    return len(rows)


@dataclass
class FetchTokenSummary:
    """Display-friendly view used by the `list` CLI command."""

    id: int
    label: str
    prefix: str
    issued_by: str | None
    expires_at: datetime
    revoked_at: datetime | None
    use_count: int
    last_used_at: datetime | None

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at > datetime.now(timezone.utc)


async def list_admin_fetch_tokens(
    session: AsyncSession, *, include_inactive: bool = False
) -> list[FetchTokenSummary]:
    stmt = select(AdminFetchToken).order_by(AdminFetchToken.created_at.desc())
    rows = (await session.scalars(stmt)).all()
    out: list[FetchTokenSummary] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        active = row.revoked_at is None and row.expires_at > now
        if not active and not include_inactive:
            continue
        out.append(
            FetchTokenSummary(
                id=row.id,
                label=row.label,
                prefix=row.token_prefix,
                issued_by=row.issued_by,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                use_count=row.use_count,
                last_used_at=row.last_used_at,
            )
        )
    return out
