"""BCP/DR: plans, tests, findings with RLS and RBAC.

docs/modules/bcp/spec.md §4 (data model) and §11 (RBAC).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class BcpPlan(Base):
    __tablename__ = "bcp_plan"
    __table_args__ = (
        Index("ix_bcp_plan_state", "org_id", "state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    scope: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    rto_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    rpo_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    procedure_refs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    dependencies: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    review_interval_days: Mapped[int] = mapped_column(Integer, default=365)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    state: Mapped[str] = mapped_column(String(16), default="active")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BcpTest(Base):
    __tablename__ = "bcp_test"
    __table_args__ = (
        Index("ix_bcp_test_plan", "org_id", "plan_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("bcp_plan.id"))
    type: Mapped[str] = mapped_column(String(16))
    scheduled_for: Mapped[datetime | None] = mapped_column(default=None)
    executed_at: Mapped[datetime | None] = mapped_column(default=None)
    executed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    outcome: Mapped[str] = mapped_column(String(10), default="pending")
    rto_met: Mapped[bool | None] = mapped_column(Boolean, default=None)
    rpo_met: Mapped[bool | None] = mapped_column(Boolean, default=None)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BcpTestStep(Base):
    __tablename__ = "bcp_test_step"
    __table_args__ = (
        Index("ix_bcp_test_step_test", "org_id", "test_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    test_id: Mapped[UUID] = mapped_column(ForeignKey("bcp_test.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(10), default="pending")
    evidence_ref: Mapped[str | None] = mapped_column(Text, default=None)


class BcpFinding(Base):
    __tablename__ = "bcp_finding"
    __table_args__ = (
        Index("ix_bcp_finding_test", "org_id", "test_id"),
        Index("ix_bcp_finding_state", "org_id", "remediation_state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    test_id: Mapped[UUID] = mapped_column(ForeignKey("bcp_test.id"))
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10))
    remediation_state: Mapped[str] = mapped_column(String(16), default="open")
    change_ref: Mapped[UUID | None] = mapped_column(default=None)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
