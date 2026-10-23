"""Clients and their sites (`docs/superpowers/specs/2026-10-23-msp-clientes-design.md` §1).

A client is a company Novieri serves; a site is one of its offices, i.e. one
LAN. Every organization is born with two system clients -- `internal` (Novieri's
own IT) and `unassigned` (tickets from unknown senders) -- created by the
`its_org_system_clients` trigger of migration 0230, not by application code, so
no path that creates an organization can forget them.

THE INTERNAL PAIR CARRIES THE ORGANIZATION'S ID: the `internal` client and its
"Principal" site both have `id = organizations.id`. Ids are UUIDs and unique per
table, so the coincidence is harmless, and it gives every caller (setup, tests,
the codemods) the internal client without a lookup.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7_pk

CLIENT_KIND_CUSTOMER = "customer"
CLIENT_KIND_INTERNAL = "internal"
CLIENT_KIND_UNASSIGNED = "unassigned"
CLIENT_KINDS = (CLIENT_KIND_CUSTOMER, CLIENT_KIND_INTERNAL, CLIENT_KIND_UNASSIGNED)

OPTIONAL_MODULES = ("budget", "bcp", "change")


class Client(TimestampMixin, Base):
    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('customer', 'internal', 'unassigned')", name="ck_clients_kind"
        ),
        CheckConstraint("status IN ('active', 'archived')", name="ck_clients_status"),
        Index("uq_clients_org_name", "org_id", text("lower(name)"), unique=True),
        Index(
            "uq_clients_one_internal",
            "org_id",
            unique=True,
            postgresql_where=text("kind = 'internal'"),
        ),
        Index(
            "uq_clients_one_unassigned",
            "org_id",
            unique=True,
            postgresql_where=text("kind = 'unassigned'"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default=CLIENT_KIND_CUSTOMER)
    status: Mapped[str] = mapped_column(String(20), default="active")
    email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(253)), default=list, server_default=text("'{}'")
    )
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    address: Mapped[str] = mapped_column(Text, default="", server_default="")
    website: Mapped[str] = mapped_column(String(300), default="", server_default="")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    enabled_modules: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), default=list, server_default=text("'{}'")
    )


class Site(TimestampMixin, Base):
    __tablename__ = "sites"
    __table_args__ = (Index("uq_sites_client_name", "client_id", text("lower(name)"), unique=True),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(Text, default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    egress_networks: Mapped[list[Any]] = mapped_column(
        ARRAY(CIDR()), default=list, server_default=text("'{}'::cidr[]")
    )
    doc_record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("doc_records.id"), default=None
    )
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
