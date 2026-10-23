"""Rollouts, rings and targets (spec-self-update.md §6.2).

Every column and index here must match migration 0095 exactly:
`tests/test_schema_drift.py` compares both sides.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

__all__ = [
    "ACTIVE_ROLLOUT_STATES",
    "COUNTED_FAILURES",
    "IN_FLIGHT",
    "UNRESOLVED",
    "AgentUpdateRing",
    "AgentUpdateRollout",
    "AgentUpdateTarget",
]

ACTIVE_ROLLOUT_STATES = ("running", "paused")
#: Target states that count toward the pause threshold (§6.3).
COUNTED_FAILURES = frozenset({"rolled_back", "failed", "refused", "lost"})
#: A job is out for these.
IN_FLIGHT = frozenset({"dispatched", "verifying"})
#: The ring is not done while any target is in one of these.
UNRESOLVED = frozenset({"pending", "dispatched", "verifying"})


class AgentUpdateRollout(Base):
    __tablename__ = "agent_update_rollouts"
    __table_args__ = (
        Index(
            "uq_agent_update_rollouts_active",
            "org_id",
            unique=True,
            postgresql_where=text("state IN ('running', 'paused')"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    target_version: Mapped[str] = mapped_column(String(64))
    is_rollback: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="draft")
    batch_size: Mapped[int] = mapped_column(Integer, default=10)
    failure_threshold_pct: Mapped[int] = mapped_column(Integer, default=5)
    health_window_s: Mapped[int] = mapped_column(Integer, default=600)
    reason: Mapped[str] = mapped_column(String(500), default="")
    pause_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    started_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)


class AgentUpdateRing(Base):
    __tablename__ = "agent_update_rings"
    __table_args__ = (
        client_org_fk("agent_update_rings"),
        UniqueConstraint("rollout_id", "ordinal", name="uq_agent_update_rings_ordinal"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    rollout_id: Mapped[UUID] = mapped_column(ForeignKey("agent_update_rollouts.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    is_pilot: Mapped[bool] = mapped_column(Boolean, default=False)
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    state: Mapped[str] = mapped_column(String(16), default="pending")
    activated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    activated_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)


class AgentUpdateTarget(Base):
    __tablename__ = "agent_update_targets"
    __table_args__ = (
        UniqueConstraint("rollout_id", "device_id", name="uq_agent_update_targets_device"),
        Index("ix_agent_update_targets_ring_state", "ring_id", "state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    rollout_id: Mapped[UUID] = mapped_column(ForeignKey("agent_update_rollouts.id"))
    ring_id: Mapped[UUID] = mapped_column(ForeignKey("agent_update_rings.id"))
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    release_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_releases.id"), default=None)
    from_version: Mapped[str] = mapped_column(String(64), default="")
    job_id: Mapped[UUID | None] = mapped_column(default=None)
    probe_job_id: Mapped[UUID | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    reason: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
