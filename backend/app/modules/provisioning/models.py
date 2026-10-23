"""Provisioning engine module tables (docs/modules/provisioning/spec.md §4).

OsImage is an immutable, org-scoped OS image suitable for provisioning;
ProvisionProfile is the provisioning policy/template per org; ProvisionRun
records a signed provision.run job's outcome. All three tables are tenant-scoped
and RLS-forced by migration 0015.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk


class OsImage(Base):
    __tablename__ = "os_image"
    __table_args__ = (Index("ix_os_image_family", "org_id", "os_family"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    os_family: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))
    artifact_ref: Mapped[str | None] = mapped_column(Text, default=None)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProvisionProfile(Base):
    __tablename__ = "provision_profile"
    __table_args__ = (Index("ix_provision_profile_enabled", "org_id", "enabled"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(255))
    image_id: Mapped[UUID] = mapped_column(ForeignKey("os_image.id"))
    post_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    target_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProvisionRun(Base):
    __tablename__ = "provision_run"
    __table_args__ = (
        Index("ix_provision_run_state", "org_id", "state"),
        client_org_fk("provision_run"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("provision_profile.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), default=None)
    target_ref: Mapped[str | None] = mapped_column(Text, default=None)
    enrollment_token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("enrollment_tokens.id"), default=None
    )
    answer_ref: Mapped[str | None] = mapped_column(Text, default=None)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    log_ref: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    actor_type: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
