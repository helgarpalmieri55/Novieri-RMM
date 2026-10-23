"""Remote control module tables (docs/modules/remote-control/spec.md §3-4).

Sessions record attended/unattended remote-access lifecycle plus recording
metadata (the recording bytes themselves live in object storage, referenced by
recording_ref). Policies are per device-group (or org-wide when
device_group_id is NULL) and gate unattended access, recording, file transfer,
and clipboard, and bound screen sessions (consent timeout, input, recording
cap). Both tables are tenant-scoped and RLS-forced by migration 0010.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7_pk

RECORDING_MAX_BYTES = 2 * 1024 * 1024 * 1024


class RemoteSession(TimestampMixin, Base):
    __tablename__ = "remote_sessions"
    __table_args__ = (
        Index("ix_remote_sessions_device", "org_id", "device_id", "id"),
        Index("ix_remote_sessions_state", "org_id", "state"),
        Index(
            "ix_remote_sessions_recording",
            "org_id",
            "ended_at",
            postgresql_where=text("recording_ref IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    technician_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    protocol: Mapped[str] = mapped_column(String(8))
    consent_mode: Mapped[str] = mapped_column(String(12))
    credential_id: Mapped[UUID | None] = mapped_column(ForeignKey("credentials.id"), default=None)
    state: Mapped[str] = mapped_column(String(20))
    relay_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    recording_ref: Mapped[str | None] = mapped_column(Text, default=None)
    recording_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    end_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    audit_event_id: Mapped[UUID | None] = mapped_column(default=None)
    # Screen sessions (migration 0100). input_enabled goes false for good when
    # the endpoint user presses Stop control.
    input_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    recording_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class RemotePolicy(TimestampMixin, Base):
    __tablename__ = "remote_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "device_group_id", name="uq_remote_policies_org_group"),
        CheckConstraint(
            "consent_timeout_s BETWEEN 15 AND 120", name="ck_remote_policies_consent_timeout"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    allow_unattended: Mapped[bool] = mapped_column(Boolean, default=False)
    record_sessions: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_file_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_clipboard: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=90)
    consent_timeout_s: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    allow_input: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    allow_privacy_blank: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    recording_max_bytes: Mapped[int] = mapped_column(
        BigInteger, default=RECORDING_MAX_BYTES, server_default=str(RECORDING_MAX_BYTES)
    )
