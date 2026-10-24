"""Declarative base and shared column helpers for the core data model."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKeyConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid_extensions import uuid7


class Base(DeclarativeBase):
    type_annotation_map = {
        UUID: PGUUID(as_uuid=True),
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


def uuid7_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def client_org_fk(table: str) -> ForeignKeyConstraint:
    """`(client_id, org_id) -> clients (id, org_id)`, migration 0231's
    `fk_<table>_client_org`: the row's client is a client of the row's own
    organization. A foreign key check ignores RLS, so under scope `all`
    nothing else stops org A's row naming org B's client. Declared on the
    model so `alembic revision --autogenerate` does not propose dropping it."""
    return ForeignKeyConstraint(
        ["client_id", "org_id"], ["clients.id", "clients.org_id"], name=f"fk_{table}_client_org"
    )


def site_client_fk(table: str) -> ForeignKeyConstraint:
    """`(site_id, client_id) -> sites (id, client_id)`, migration 0231's
    `fk_<table>_site_client`: the row's site is one of the row's client's,
    and a site holding such a row cannot change client."""
    return ForeignKeyConstraint(
        ["site_id", "client_id"], ["sites.id", "sites.client_id"], name=f"fk_{table}_site_client"
    )
