"""Unit tests for app.services.tokens.

Pure primitives first (no DB), then DB-touching helpers against an in-memory
async SQLite database so CI doesn't need Postgres.

Note: the SQLite path uses plain Text columns for timestamps. Python datetimes
round-trip fine; we just lose the pg-specific TZ handling. That's OK for the
unit tests here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import AdminFetchToken
from app.models.base import Base
from app.services.tokens import (
    find_valid_admin_token,
    hash_token,
    issue_admin_fetch_token,
    list_admin_fetch_tokens,
    mint_token,
    prefix_of,
    revoke_admin_fetch_token,
    verify_token,
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def test_mint_token_is_unique_and_long() -> None:
    samples = {mint_token() for _ in range(200)}
    assert len(samples) == 200
    # URL-safe base64 of 32 bytes is 43 chars
    assert all(len(s) >= 40 for s in samples)


def test_hash_is_deterministic_and_verify_is_constant_time() -> None:
    raw = "abcdef-1234"
    h = hash_token(raw)
    assert h == hash_token(raw)
    assert verify_token(raw, h) is True
    assert verify_token(raw + "x", h) is False


def test_prefix_of() -> None:
    assert prefix_of("abcdefghijklmnop") == "abcdefgh"


# ---------------------------------------------------------------------------
# DB-backed fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Fresh in-memory SQLite DB per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # AdminFetchToken is all we need; Base.metadata contains every model,
        # but SQLite can't do pg_trgm / JSONB / INET - create only the table
        # under test.
        await conn.run_sync(
            lambda sync_conn: AdminFetchToken.__table__.create(sync_conn)
        )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# issue_admin_fetch_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_stores_hash_not_raw(session: AsyncSession) -> None:
    result = await issue_admin_fetch_token(
        session, label="bench-1", ttl_hours=24, issued_by="aaron"
    )
    await session.commit()

    # Raw token was returned
    assert len(result.raw) >= 40
    # Stored row has the HASH, not the raw token
    assert result.row.token_hash == hash_token(result.raw)
    assert result.row.token_hash != result.raw
    # Prefix is stored verbatim
    assert result.row.token_prefix == result.raw[:8]
    # TTL applied
    assert result.row.expires_at > datetime.now(timezone.utc)
    assert result.row.expires_at < datetime.now(timezone.utc) + timedelta(hours=25)
    # Metadata
    assert result.row.label == "bench-1"
    assert result.row.issued_by == "aaron"


@pytest.mark.asyncio
async def test_issue_rejects_bad_ttl_and_blank_label(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="ttl_hours"):
        await issue_admin_fetch_token(session, label="x", ttl_hours=0)
    with pytest.raises(ValueError, match="label"):
        await issue_admin_fetch_token(session, label="   ", ttl_hours=1)


# ---------------------------------------------------------------------------
# find_valid_admin_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_valid_returns_row_for_correct_raw(session: AsyncSession) -> None:
    result = await issue_admin_fetch_token(session, label="l", ttl_hours=1)
    await session.commit()

    found = await find_valid_admin_token(session, result.raw)
    assert found is not None
    assert found.id == result.row.id


@pytest.mark.asyncio
async def test_find_valid_returns_none_for_wrong_raw(session: AsyncSession) -> None:
    await issue_admin_fetch_token(session, label="l", ttl_hours=1)
    await session.commit()

    assert await find_valid_admin_token(session, "not-a-real-token-xxxxxxxx") is None


@pytest.mark.asyncio
async def test_find_valid_returns_none_when_expired(session: AsyncSession) -> None:
    result = await issue_admin_fetch_token(session, label="l", ttl_hours=1)
    # Backdate the expiry.
    result.row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    assert await find_valid_admin_token(session, result.raw) is None


@pytest.mark.asyncio
async def test_find_valid_returns_none_when_revoked(session: AsyncSession) -> None:
    result = await issue_admin_fetch_token(session, label="l", ttl_hours=1)
    result.row.revoked_at = datetime.now(timezone.utc)
    await session.commit()

    assert await find_valid_admin_token(session, result.raw) is None


# ---------------------------------------------------------------------------
# revoke_admin_fetch_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revoke_by_label_marks_only_active(session: AsyncSession) -> None:
    a = await issue_admin_fetch_token(session, label="bench-2", ttl_hours=1)
    b = await issue_admin_fetch_token(session, label="bench-2", ttl_hours=1)
    other = await issue_admin_fetch_token(session, label="bench-3", ttl_hours=1)
    await session.commit()

    count = await revoke_admin_fetch_token(session, "bench-2")
    await session.commit()

    assert count == 2
    # A and B now fail the valid check; "other" still works
    assert await find_valid_admin_token(session, a.raw) is None
    assert await find_valid_admin_token(session, b.raw) is None
    assert await find_valid_admin_token(session, other.raw) is not None


@pytest.mark.asyncio
async def test_revoke_by_prefix_works(session: AsyncSession) -> None:
    r = await issue_admin_fetch_token(session, label="whatever", ttl_hours=1)
    await session.commit()

    count = await revoke_admin_fetch_token(session, r.row.token_prefix)
    await session.commit()

    assert count == 1
    assert await find_valid_admin_token(session, r.raw) is None


@pytest.mark.asyncio
async def test_revoke_nonmatching_returns_zero(session: AsyncSession) -> None:
    count = await revoke_admin_fetch_token(session, "does-not-exist")
    assert count == 0


# ---------------------------------------------------------------------------
# list_admin_fetch_tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_default_hides_inactive(session: AsyncSession) -> None:
    active = await issue_admin_fetch_token(session, label="A", ttl_hours=1)
    rev = await issue_admin_fetch_token(session, label="R", ttl_hours=1)
    rev.row.revoked_at = datetime.now(timezone.utc)
    await session.commit()

    active_only = await list_admin_fetch_tokens(session)
    assert len(active_only) == 1
    assert active_only[0].label == "A"

    all_rows = await list_admin_fetch_tokens(session, include_inactive=True)
    labels = {r.label for r in all_rows}
    assert labels == {"A", "R"}
