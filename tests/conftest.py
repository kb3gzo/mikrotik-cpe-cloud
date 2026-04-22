"""Shared test config.

The production schema uses Postgres-specific column types (INET, MACADDR,
JSONB) that the SQLite dialect can't render on its own. Our tests run
against an in-memory aiosqlite DB, so we teach SQLAlchemy to emit these as
plain TEXT on SQLite. We also collapse BigInteger to INTEGER on SQLite so
primary keys become ROWID aliases (autoincrement works).
"""
from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR
from sqlalchemy.ext.compiler import compiles


@compiles(INET, "sqlite")
def _compile_inet_sqlite(element, compiler, **kw):
    return "TEXT"


@compiles(MACADDR, "sqlite")
def _compile_macaddr_sqlite(element, compiler, **kw):
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):
    # SQLite only promotes INTEGER PRIMARY KEY to ROWID (which auto-populates
    # on INSERT). BIGINT does NOT act as a ROWID alias, so autoincrement
    # silently no-ops and INSERTs fail the NOT NULL on id. Collapsing to
    # INTEGER under the sqlite dialect fixes it; semantics are identical at
    # our scale.
    return "INTEGER"
