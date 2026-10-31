"""Backup engine module tables (docs/modules/backup/spec.md §4).

BackupRepository is ONE client's dedup/encrypted storage target (migration
0242: its key travels to every device that uses it, so it is never shared);
BackupPolicy governs sources/schedule/retention per device-group, or per
client when device_group_id is NULL; BackupRun records a signed backup.run job's
outcome; BackupPoint is a completed, restorable snapshot produced by a
successful run; RestoreJob records a signed backup.restore job (restore or
proven-restore/verify). All five tables are tenant-scoped and RLS-forced by
migration 0014.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
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

#: `backup_repositories.state`, which no CHECK constraint holds (0014). A new
#: repository is `active`. A `retired` one (phase 3 design §5.5) takes no new
#: backup -- the dispatch refuses it, `dedup.py` grants it no writes, no enabled
#: policy may name it -- and its points stay restorable (`read.py`). There is no
#: way back to `active` in this phase.
REPOSITORY_ACTIVE = "active"
REPOSITORY_RETIRED = "retired"


class BackupRepository(TimestampMixin, Base):
    __tablename__ = "backup_repositories"
    __table_args__ = (
        client_org_fk("backup_repositories"),
        # A name is one client's (migration 0242, plan 2026-10-26 Task 13).
        UniqueConstraint(
            "org_id", "client_id", "name", name="uq_backup_repositories_client_name"
        ),
        # What a run's and a point's `(repository_id, client_id)` reference:
        # a run or point names a repository of its own device's client.
        UniqueConstraint("id", "client_id", name="uq_backup_repositories_id_client"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C), plan 2026-10-26 C1: the ONE client whose
    # devices back up here, set by the application from the request. Its
    # master key travels in their signed jobs and its chunks are deduplicated
    # here, so a repository shared by two clients would hand one the other's
    # key.
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    storage_ref: Mapped[str] = mapped_column(Text)
    encryption_key_ref: Mapped[str] = mapped_column(Text)
    dedup_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # REPOSITORY_ACTIVE or REPOSITORY_RETIRED (above).
    state: Mapped[str] = mapped_column(String(16), default="active")
    # Set = off-site copying is on for this repository (migration 0090).
    offsite_target_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("backup_offsite_targets.id"), default=None
    )


class BackupChunk(TimestampMixin, Base):
    """One row per chunk an agent has reported stored in one repository —
    the index the server-side dedup exchange answers from (spec §5.2,
    migration 0053).

    A row means "this chunk is in this repository". It is created only under
    the `(org_id, repository_id)` the server resolved from the signed
    `backup.run` job, never from a repository the agent named, so a row can
    never land in another tenant's repository. `UNIQUE (repository_id,
    chunk_id)` and NOT `(org_id, repository_id, chunk_id)`: repository ids are
    already globally unique, so the wider form would read as stricter while
    being strictly weaker (0050_site_server.py's `UNIQUE (device_id)`
    reasoning). `org_id` is still carried, still an explicit predicate on
    every query (invariant #5), and still what RLS enforces on.
    """

    __tablename__ = "backup_chunks"
    __table_args__ = (
        UniqueConstraint("repository_id", "chunk_id", name="uq_backup_chunks_repo_chunk"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the backup_repositories row named by repository_id (migration 0242):
    # never sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("backup_repositories.id"))
    chunk_id: Mapped[str] = mapped_column(String(64))


class BackupPolicy(TimestampMixin, Base):
    __tablename__ = "backup_policies"
    __table_args__ = (client_org_fk("backup_policies"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT. With no
    # group it is the application's: that client's default policy. A policy
    # names only a repository of its own client, so one of every client
    # names none (`its_backup_policy_repository_guard`, migration 0242).
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("backup_repositories.id"))
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    window_cron: Mapped[str | None] = mapped_column(String(120), default=None)
    retention_gfs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class BackupRun(TimestampMixin, Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        Index("ix_backup_runs_state", "org_id", "state"),
        # The repository is of the run's (its device's) client (migration
        # 0242). NOT VALID in the database: rows from before it are history;
        # every new row is checked -- and so is an old one that the same
        # transaction already wrote once, so write such a row once.
        ForeignKeyConstraint(
            ["repository_id", "client_id"],
            ["backup_repositories.id", "backup_repositories.client_id"],
            name="fk_backup_runs_repository_client",
        ),
    )
    # state: pending | running | success | failed | missed

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
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("backup_repositories.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    bytes_protected: Mapped[int | None] = mapped_column(BigInteger, default=None)
    bytes_stored: Mapped[int | None] = mapped_column(BigInteger, default=None)
    chunk_count: Mapped[int | None] = mapped_column(Integer, default=None)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    actor_type: Mapped[str] = mapped_column(String(10))
    # Format-2 result members (migration 0092); NULL from older agents.
    consistency: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, default=None)


class BackupPoint(TimestampMixin, Base):
    __tablename__ = "backup_points"
    __table_args__ = (
        Index("ix_backup_points_device", "org_id", "device_id", "taken_at"),
        Index("ix_backup_points_consistency", "org_id", "consistency_level"),
        # As on `backup_runs` (migration 0242, NOT VALID).
        ForeignKeyConstraint(
            ["repository_id", "client_id"],
            ["backup_repositories.id", "backup_repositories.client_id"],
            name="fk_backup_points_repository_client",
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
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("backup_repositories.id"))
    backup_run_id: Mapped[UUID] = mapped_column(ForeignKey("backup_runs.id"))
    taken_at: Mapped[datetime] = mapped_column(server_default=func.now())
    manifest_ref: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    dedup_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(16))
    verified_at: Mapped[datetime | None] = mapped_column(default=None)
    restore_tested_at: Mapped[datetime | None] = mapped_column(default=None)
    # What the last test restore proved: level `read`, scope `sampled` | `full`.
    restore_test_level: Mapped[str | None] = mapped_column(String(12), default=None)
    restore_test_scope: Mapped[str | None] = mapped_column(String(12), default=None)
    consistency: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    consistency_level: Mapped[str | None] = mapped_column(String(16), default=None)
    crash_consistent: Mapped[bool | None] = mapped_column(Boolean, default=None)
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, default=None)


class RestoreJob(TimestampMixin, Base):
    __tablename__ = "restore_jobs"
    __table_args__ = (Index("ix_restore_jobs_state", "org_id", "state"),)

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
    backup_point_id: Mapped[UUID] = mapped_column(ForeignKey("backup_points.id"))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    mode: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    restored_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    actor_type: Mapped[str] = mapped_column(String(10))
    warnings: Mapped[list[str] | None] = mapped_column(JSONB, default=None)


class BackupOffsiteTarget(Base):
    """An S3-compatible bucket that holds the off-site copy (spec-server-backup
    §6.2). The secret is a vault-sealed `credentials` row of type
    `s3_offsite`; only the replication worker decrypts it."""

    __tablename__ = "backup_offsite_targets"
    __table_args__ = (
        client_org_fk("backup_offsite_targets"),
        # Per client, the global rows among themselves (migration 0231, Task 8b).
        UniqueConstraint(
            "org_id",
            "client_id",
            "name",
            name="uq_backup_offsite_targets_client_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    endpoint: Mapped[str | None] = mapped_column(Text, default=None)
    region: Mapped[str] = mapped_column(String(64), default="us-east-1")
    bucket: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(255), default="")
    addressing: Mapped[str] = mapped_column(String(8), default="path")
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id"))
    lock_mode: Mapped[str] = mapped_column(String(12), default="compliance")
    immutable_days: Mapped[int] = mapped_column(Integer, default=30)
    retention_gfs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    max_mbps: Mapped[int | None] = mapped_column(Integer, default=None)
    state: Mapped[str] = mapped_column(String(12), default="untested")
    test_requested_at: Mapped[datetime | None] = mapped_column(default=None)
    last_test: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_ok_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BackupPointCopy(Base):
    """One backup point's copy on one off-site target (§6.3)."""

    __tablename__ = "backup_point_copies"
    __table_args__ = (
        UniqueConstraint("point_id", "target_id", name="uq_backup_point_copies_point_target"),
        Index("ix_backup_point_copies_target_state", "org_id", "target_id", "state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the backup_points row named by point_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    point_id: Mapped[UUID] = mapped_column(ForeignKey("backup_points.id"))
    target_id: Mapped[UUID] = mapped_column(ForeignKey("backup_offsite_targets.id"))
    state: Mapped[str] = mapped_column(String(12), default="pending")
    copied_at: Mapped[datetime | None] = mapped_column(default=None)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    chunks_copied: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    manifest_object: Mapped[str | None] = mapped_column(Text, default=None)
    manifest_version_id: Mapped[str | None] = mapped_column(Text, default=None)
    catalog_version_id: Mapped[str | None] = mapped_column(Text, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BackupChunkCopy(Base):
    """One chunk object on one off-site target; the row is what makes
    replication resumable and off-site GC possible (§6.3, §6.6)."""

    __tablename__ = "backup_chunk_copies"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "repository_id", "chunk_id", name="uq_backup_chunk_copies_target_chunk"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the backup_repositories row named by repository_id (migration 0242):
    # never sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("backup_repositories.id"))
    target_id: Mapped[UUID] = mapped_column(ForeignKey("backup_offsite_targets.id"))
    chunk_id: Mapped[str] = mapped_column(String(64))
    object_name: Mapped[str] = mapped_column(Text)
    version_id: Mapped[str | None] = mapped_column(Text, default=None)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    verified_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BackupRecoveryKey(Base):
    """An operator-held X25519 public key every repository key is wrapped to
    off-site (§6.5). The private key never reaches the server."""

    __tablename__ = "backup_recovery_keys"
    __table_args__ = (
        client_org_fk("backup_recovery_keys"),
        # Per client, the global rows among themselves (migration 0231, Task 8b).
        Index(
            "uq_backup_recovery_keys_client_active",
            "org_id",
            "client_id",
            "fingerprint",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    public_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    challenge_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    challenge_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    proven_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(default=None)
