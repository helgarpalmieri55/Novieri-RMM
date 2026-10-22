"""Alembic environment. The migration URL comes from ITS_ALEMBIC_URL (admin role),
falling back to ITS_DATABASE_ADMIN_URL. Migrations always run as the schema-owning
admin role, never as the RLS-constrained app role."""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (imports register all model metadata)
from app.models.base import Base

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("ITS_ALEMBIC_URL") or os.environ.get("ITS_DATABASE_ADMIN_URL")
    if not url:
        raise RuntimeError("Set ITS_ALEMBIC_URL or ITS_DATABASE_ADMIN_URL to run migrations")
    return url


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
