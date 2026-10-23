"""Application deployment module tables (docs/modules/app-deploy/spec.md §4).

Packages are the org-scoped catalog of installable software per OS family;
Deployment targets a device or group with an install/update/uninstall action;
DeploymentResult tracks the per-device outcome of a deployment; Script is the
org-scoped catalog of signed-job script bodies; ScriptRun records the outcome
of a script.run job on a device. All five tables are tenant-scoped and
RLS-forced by migration 0013.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7_pk


class Package(TimestampMixin, Base):
    __tablename__ = "packages"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "name", "version", "os_family", name="uq_packages_org_name_version_os"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    os_family: Mapped[str] = mapped_column(String(8))
    installer_type: Mapped[str] = mapped_column(String(20))
    artifact_ref: Mapped[str | None] = mapped_column(Text, default=None)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # The identifier a PACKAGE MANAGER knows this package by ("Mozilla.Firefox",
    # "googlechrome"). Distinct from `name`, which is the display name an
    # operator reads, and from `detection_rule["name"]`, which is matched
    # against installed-software display names. Nullable because every row
    # cataloged before migration 0052 has one and no backfill can invent it;
    # `artifacts.manager_package_identifier` falls back to
    # `detection_rule["name"]`, which is exactly what the agent derived on its
    # own before this field existed.
    package_identifier: Mapped[str | None] = mapped_column(String(200), default=None)
    detection_rule: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    uninstall_rule: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    install_args: Mapped[str | None] = mapped_column(Text, default=None)


class Deployment(TimestampMixin, Base):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_state", "org_id", "state"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    package_id: Mapped[UUID] = mapped_column(ForeignKey("packages.id"))
    action: Mapped[str] = mapped_column(String(12))
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column()
    window_cron: Mapped[str | None] = mapped_column(String(120), default=None)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    state: Mapped[str] = mapped_column(String(16), default="pending")


class DeploymentResult(TimestampMixin, Base):
    __tablename__ = "deployment_results"
    __table_args__ = (
        UniqueConstraint("deployment_id", "device_id", name="uq_deployment_results_dep_device"),
        Index("ix_deployment_results_state", "org_id", "state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("deployments.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    log_ref: Mapped[str | None] = mapped_column(Text, default=None)
    detected_after: Mapped[bool | None] = mapped_column(Boolean, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


class Script(TimestampMixin, Base):
    __tablename__ = "scripts"
    __table_args__ = (
        UniqueConstraint("org_id", "name", "version", name="uq_scripts_org_name_version"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    os_family: Mapped[str] = mapped_column(String(8))
    interpreter: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)


class ScriptRun(TimestampMixin, Base):
    __tablename__ = "script_runs"
    __table_args__ = (
        UniqueConstraint(
            "script_id",
            "device_id",
            "requested_by",
            "created_at",
            name="uq_script_runs_script_device_requester_created",
        ),
        Index("ix_script_runs_state", "org_id", "state"),
        Index("ix_script_runs_policy", "org_id", "policy_id", "device_id", "created_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id"))
    script_version: Mapped[int] = mapped_column(Integer)
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column()
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    log_ref: Mapped[str | None] = mapped_column(Text, default=None)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    # Migration 0121: the schedule that dispatched it, and what it printed
    # (bounded; cleared past the policy's keep_runs).
    policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("script_policies.id"), default=None
    )
    output: Mapped[str | None] = mapped_column(Text, default=None)


class ScriptPolicy(TimestampMixin, Base):
    """Run a script on every device in a group, on a schedule (migration 0121).

    The sweep dispatches the ordinary signed `script.run` with the script's
    current body; `interval_hours` is the gap between runs on one device, and
    `in_maintenance_window` holds each run to that device's patch window.
    """

    __tablename__ = "script_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_script_policies_org_name"),
        CheckConstraint("interval_hours BETWEEN 1 AND 720", name="ck_script_policies_interval"),
        CheckConstraint("keep_runs BETWEEN 1 AND 500", name="ck_script_policies_keep_runs"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the device_groups row named by device_group_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    name: Mapped[str] = mapped_column(String(200))
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id"))
    device_group_id: Mapped[UUID] = mapped_column(ForeignKey("device_groups.id"))
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    in_maintenance_window: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_on_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    keep_runs: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
