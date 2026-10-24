"""Patch management module tables (docs/modules/patch/spec.md).

Patches are the org-scoped catalog of known updates per OS family/source;
DevicePatchStatus tracks per-device applicability/state; PatchPolicy governs
rings/reboot/scan cadence per device-group (or org-wide when device_group_id
is NULL); PatchApproval gates a patch's rollout into a given ring; PatchRun
records the outcome of a signed patch-apply job on a device. All five tables
are tenant-scoped and RLS-forced by migration 0011.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, client_org_fk, uuid7_pk


class Patch(TimestampMixin, Base):
    __tablename__ = "patches"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "os_family", "source", "identifier", name="uq_patches_org_os_src_id"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    os_family: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(20))
    identifier: Mapped[str] = mapped_column(String(400))
    title: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(12))
    kb: Mapped[str | None] = mapped_column(String(40), default=None)
    cve: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    superseded_by: Mapped[UUID | None] = mapped_column(ForeignKey("patches.id"), default=None)
    priority: Mapped[int | None] = mapped_column(Integer, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DevicePatchStatus(TimestampMixin, Base):
    __tablename__ = "device_patch_status"
    __table_args__ = (
        UniqueConstraint("device_id", "patch_id", name="uq_dps_device_patch"),
        Index("ix_dps_state", "org_id", "state"),
        Index("ix_dps_patch", "org_id", "patch_id"),
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
    patch_id: Mapped[UUID] = mapped_column(ForeignKey("patches.id"))
    state: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class PatchPolicy(TimestampMixin, Base):
    __tablename__ = "patch_policies"
    __table_args__ = (
        client_org_fk("patch_policies"),
        UniqueConstraint("org_id", "device_group_id", name="uq_patch_policies_org_group"),
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
    ring_name: Mapped[str] = mapped_column(String(40), default="broad")
    ring_order: Mapped[int] = mapped_column(Integer, default=100)
    maintenance_window: Mapped[str | None] = mapped_column(String(120), default=None)
    auto_approve_security: Mapped[bool] = mapped_column(Boolean, default=False)
    reboot: Mapped[str] = mapped_column(String(12), default="if_required")
    reboot_deferral_hours: Mapped[int] = mapped_column(Integer, default=24)
    scan_interval_hours: Mapped[int] = mapped_column(Integer, default=24)


class PatchApproval(TimestampMixin, Base):
    __tablename__ = "patch_approvals"
    __table_args__ = (
        client_org_fk("patch_approvals"),
        # Per client, the global rows among themselves (migration 0231, Task 8b).
        UniqueConstraint(
            "org_id",
            "client_id",
            "patch_id",
            "ring_name",
            name="uq_patch_approvals_client_patch_ring",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    patch_id: Mapped[UUID] = mapped_column(ForeignKey("patches.id"))
    ring_name: Mapped[str] = mapped_column(String(40))
    # NULL means a system auto-approval (PD-02); see migration 0012.
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    approved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class PatchReboot(TimestampMixin, Base):
    """One restart a device owes after patching (migration 0120).

    `pending` waits for the maintenance window; `dispatched` has a signed
    `patch.reboot` on its way; `scheduled` means the OS has a restart booked
    for `scheduled_for`; `completed` is confirmed by a later scan whose boot
    time is after the record; `cancelled` and `failed` say why it stopped.
    At most one open record per device (partial unique index).
    """

    __tablename__ = "patch_reboots"
    __table_args__ = (
        Index(
            "uq_patch_reboots_open",
            "org_id",
            "device_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'dispatched', 'scheduled')"),
        ),
        Index("ix_patch_reboots_state", "org_id", "state"),
        CheckConstraint(
            "state IN ('pending', 'dispatched', 'scheduled', 'completed', 'cancelled', 'failed')",
            name="ck_patch_reboots_state",
        ),
        CheckConstraint(
            "origin IN ('apply', 'scan', 'operator')", name="ck_patch_reboots_origin"
        ),
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
    origin: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(12), default="pending")
    # The apply job that asked for it, when there was one.
    apply_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    # The latest `patch.reboot` job dispatched for it.
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_for: Mapped[datetime | None] = mapped_column(default=None)
    user_present: Mapped[bool | None] = mapped_column(Boolean, default=None)
    verify_requested_at: Mapped[datetime | None] = mapped_column(default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)


class PatchRun(TimestampMixin, Base):
    __tablename__ = "patch_runs"
    __table_args__ = (
        Index("ix_patch_runs_device", "org_id", "device_id", "id"),
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
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reboot_required: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
