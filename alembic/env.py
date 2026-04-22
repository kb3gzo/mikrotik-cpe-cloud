"""Alembic environment — targets app.models.Base.metadata.

Runs migrations synchronously. Even though the app uses the async psycopg
driver, Alembic is fine with a sync URL. We swap `postgresql+psycopg` →
`postgresql+psycopg` (sync driver handles both) below.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base  # noqa: F401 — imports all ORM models to populate metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _sync_url() -> str:
    """Alembic uses a sync connection. psycopg3 handles both sync and async
    with the same URL scheme (`postgresql+psycopg://...`), so no rewrite is
    needed — but if someone sets an `asyncpg` URL, swap it here.
    """
    url = get_settings().database_url
    return url.replace("+asyncpg", "+psycopg")


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection."""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
