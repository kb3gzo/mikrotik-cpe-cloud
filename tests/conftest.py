"""Shared test config.

The production schema uses Postgres-specific column types (``INET``,
``MACADDR``, ``JSONB``) that the SQLite dialect can't render on its own.
Our tests run against an in-memory ``aiosqlite`` DB, so we teach SQLAlchemy
to emit these as plain ``TEXT`` on SQLite — the semantics we need for a
test (uniqueness, equality) are preserved; we lose Postgres-native
validation but tests don't rely on it.

Importing this ``conftest`` is enough; the ``@compiles`` decorators register
themselves as a side-effect the first time the types are referenced.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import INET, JSONB, MACADDR
from sqlalchemy.ext.compiler import compiles


@compiles(INET, "sqlite")
def _compile_inet_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "TEXT"


@compiles(MACADDR, "sqlite")
def _compile_macaddr_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "TEXT"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "TEXT"
