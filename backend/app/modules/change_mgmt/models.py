"""Change management module tables (docs/modules/change_management/spec.md §4).

ChangeRequest is a change control request with approvals and linked actions;
ChangeApproval records a decision; ChangeLink binds a change to a job or other
action; ChangeExternalRef tracks synchronization with external systems.
All four tables are tenant-scoped and RLS-forced by migration 0016.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk


class ChangeRequest(Base):
    __tablename__ = "change_request"
    __table_args__ = (
        Index("ix_change_request_state", "org_id", "state"),
        Index("ix_change_request_window", "org_id", "window_start"),
        CheckConstraint("origin IN ('manual', 'auto')", name="ck_change_request_origin"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    type: Mapped[str] = mapped_column(String(12))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(8))
    affected: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    window_start: Mapped[datetime | None] = mapped_column(default=None)
    window_end: Mapped[datetime | None] = mapped_column(default=None)
    rollback_plan: Mapped[str | None] = mapped_column(Text, default=None)
    state: Mapped[str] = mapped_column(String(16), default="draft")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Migration 0122: "auto" is the evidence record written for a patch
    # approval or deployment made with no change and no gate.
    origin: Mapped[str] = mapped_column(String(8), server_default="manual", default="manual")


class ChangeApproval(Base):
    __tablename__ = "change_approval"
    __table_args__ = (Index("ix_change_approval_change", "org_id", "change_id"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    change_id: Mapped[UUID] = mapped_column(ForeignKey("change_request.id"))
    approver_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(10))
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    is_retrospective: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChangeLink(Base):
    __tablename__ = "change_link"
    __table_args__ = (
        UniqueConstraint("org_id", "change_id", "action_type", "action_id", name="uq_change_link"),
        Index("ix_change_link_change", "org_id", "change_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    change_id: Mapped[UUID] = mapped_column(ForeignKey("change_request.id"))
    action_type: Mapped[str] = mapped_column(String(20))
    action_id: Mapped[UUID]
    linked_at: Mapped[datetime] = mapped_column(server_default=func.now())
    linked_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))


class ChangeExternalRef(Base):
    __tablename__ = "change_external_ref"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    change_id: Mapped[UUID] = mapped_column(ForeignKey("change_request.id"))
    system: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(default=None)


class ChangeGate(Base):
    """Patch approvals and/or deployments reaching a device group (or, with
    no group, every device) need a linked change that is approved or being
    implemented (migration 0122)."""

    __tablename__ = "change_gates"
    __table_args__ = (
        client_org_fk("change_gates"),
        Index(
            "uq_change_gates_org_group",
            "org_id",
            "device_group_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    applies_to_patches: Mapped[bool] = mapped_column(Boolean, default=True)
    applies_to_deployments: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
