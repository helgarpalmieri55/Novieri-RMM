"""Licenses/SAM: vendor, product, license, allocation, snapshot, ignore.

docs/modules/licenses/spec.md §2 (data model) and §9 (security invariants).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk


class Vendor(Base):
    __tablename__ = "vendor"
    __table_args__ = (
        Index("ix_vendor_org_name", "org_id", "name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(String(255), default=None)
    contacts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SoftwareProduct(Base):
    __tablename__ = "software_product"
    __table_args__ = (
        Index("ix_software_product_org_name", "org_id", "name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(255))
    publisher: Mapped[str] = mapped_column(String(255), default="")
    match_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class License(Base):
    __tablename__ = "license"
    __table_args__ = (
        Index("ix_license_org_product", "org_id", "product_id"),
        Index("ix_license_org_renews_at", "org_id", "renews_at"),
        Index(
            "ix_license_org_maintenance_expires_at",
            "org_id",
            "maintenance_expires_at",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    product_id: Mapped[UUID] = mapped_column(ForeignKey("software_product.id"))
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendor.id"), default=None)
    type: Mapped[str] = mapped_column(String(16))
    entitlements: Mapped[int] = mapped_column(Integer)
    key_sealed: Mapped[str | None] = mapped_column(Text, default=None)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    po_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    purchased_at: Mapped[date | None] = mapped_column(Date, default=None)
    maintenance_expires_at: Mapped[date | None] = mapped_column(Date, default=None)
    renews_at: Mapped[date | None] = mapped_column(Date, default=None)
    agreement_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LicenseAllocation(Base):
    __tablename__ = "license_allocation"
    __table_args__ = (
        Index("ix_license_allocation_org_license", "org_id", "license_id"),
        Index("ix_license_allocation_org_person", "org_id", "person_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    license_id: Mapped[UUID] = mapped_column(ForeignKey("license.id"))
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column()
    # Set for `person` targets, and for `user` targets from that account's
    # person (docs/modules/people/spec.md §0.4). NULL for device targets.
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("person.id"), default=None)
    allocated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class LicenseComplianceSnapshot(Base):
    __tablename__ = "license_compliance_snapshot"
    __table_args__ = (
        Index(
            "ix_license_compliance_snapshot_org_product_computed",
            "org_id",
            "product_id",
            "computed_at",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    product_id: Mapped[UUID] = mapped_column(ForeignKey("software_product.id"))
    entitled: Mapped[int] = mapped_column(Integer)
    installed: Mapped[int] = mapped_column(Integer)
    delta: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SoftwareIgnore(Base):
    __tablename__ = "software_ignore"
    __table_args__ = (
        client_org_fk("software_ignore"),
        UniqueConstraint("org_id", "name", name="uq_software_ignore_org_name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
