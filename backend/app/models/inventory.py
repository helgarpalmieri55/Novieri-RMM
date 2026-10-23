"""Inventory module tables (docs/modules/inventory/spec.md §4).

Module tables reference core entities by device_id/org_id and never duplicate
core identity. All four are tenant-scoped and RLS-forced by migration 0005.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, FetchedValue, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        Index("ix_inventory_snapshots_device", "device_id", "id"),
        # `(org_id, id)`, ADDED BY MIGRATION 0081 FOR ONE QUERY AND DECLARED
        # HERE BECAUSE THE MODEL IS THE SCHEMA OF RECORD. The device-group
        # re-evaluation sweep reads "the newest snapshot id this org holds" at
        # the start of every run, as its incremental watermark. Without this
        # index that is a backward walk of the primary key filtering on
        # `org_id` -- and for a tenant that has ingested nothing while its
        # neighbours have, the walk is the whole table.
        #
        # DECLARED, NOT ONLY CREATED. `tests/test_schema_drift.py` compares
        # both sets, and an index the model has never heard of comes back from
        # the next `alembic revision --autogenerate` as a bare
        # `op.drop_index` -- which is how `ix_doc_articles_fts` was deleted for
        # a month.
        Index("ix_inventory_snapshots_org_id", "org_id", "id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    taken_at: Mapped[datetime] = mapped_column()
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
    software_count: Mapped[int] = mapped_column(default=0)
    changes_count: Mapped[int] = mapped_column(default=0)


class InstalledSoftware(Base):
    __tablename__ = "installed_software"
    __table_args__ = (
        UniqueConstraint("device_id", "name"),
        Index("ix_installed_software_org_name", "org_id", "name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    name: Mapped[str] = mapped_column(String(300))
    version: Mapped[str] = mapped_column(String(100), default="")
    publisher: Mapped[str] = mapped_column(String(200), default="")
    install_date: Mapped[date | None] = mapped_column(Date, default=None)


class InventoryChange(Base):
    __tablename__ = "inventory_changes"
    __table_args__ = (Index("ix_inventory_changes_device", "org_id", "device_id", "id"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    ts: Mapped[datetime] = mapped_column()
    change_type: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(300))
    from_value: Mapped[str | None] = mapped_column(Text, default=None)
    to_value: Mapped[str | None] = mapped_column(Text, default=None)


class HardwareComponent(Base):
    __tablename__ = "hardware_components"
    __table_args__ = (UniqueConstraint("device_id", "type", "identifier"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    type: Mapped[str] = mapped_column(String(20))
    identifier: Mapped[str] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
