"""API-layer request/response models for the BCP/DR module's HTTP routes
(docs/modules/bcp/spec.md).

These are plain API-layer request/response shapes, not wire contracts —
this module dispatches no agent jobs, so there is no signed-job params/
result pair here and no `extra="forbid"` base is needed. Per security
invariant #3, no model in this file carries a secret value: BCP/DR has no
credential fields of its own.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class PlanCreateRequest(BaseModel):
    # The client the plan is for (MSP clients plan 2026-10-26 Task 7): it must
    # use continuity, and the plan's scope, procedures and findings' changes
    # are its. Fixed for the plan's life -- no update carries it.
    client_id: UUID
    name: str
    description: str = ""
    scope: list[dict[str, Any]] = []
    rto_minutes: int | None = None
    rpo_minutes: int | None = None
    procedure_refs: list[str] = []
    dependencies: list[dict[str, Any]] = []
    review_interval_days: int = 365


class PlanUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    scope: list[dict[str, Any]] | None = None
    rto_minutes: int | None = None
    rpo_minutes: int | None = None
    procedure_refs: list[str] | None = None
    dependencies: list[dict[str, Any]] | None = None
    review_interval_days: int | None = None
    # Lifecycle state. `archived` retires a plan from active DR coverage
    # without deleting its historical tests/evidence. The service validates
    # this against `_VALID_PLAN_STATES` ({"active", "archived"}).
    state: Literal["active", "archived"] | None = None


class PlanOut(BaseModel):
    id: UUID
    client_id: UUID
    name: str
    description: str
    scope: list[dict[str, Any]]
    rto_minutes: int | None
    rpo_minutes: int | None
    procedure_refs: list[str]
    dependencies: list[dict[str, Any]]
    review_interval_days: int
    last_reviewed_at: datetime | None
    approved_by: UUID | None
    state: str
    created_by: UUID
    created_at: datetime


class PlanStatusOut(BaseModel):
    id: UUID
    name: str
    state: str
    review_overdue: bool
    last_reviewed_at: datetime | None
    last_test_outcome: str | None
    open_findings: int
    restore_proven_coverage: dict[str, Any]


class TestCreateRequest(BaseModel):
    __test__ = False  # not a pytest test class
    type: Literal["restore_drill", "failover", "tabletop", "other"]
    scheduled_for: datetime | None = None
    notes: str = ""


class TestRecordRequest(BaseModel):
    __test__ = False  # not a pytest test class
    executed_at: datetime | None = None
    rto_met: bool | None = None
    rpo_met: bool | None = None
    outcome: Literal["pass", "partial", "fail"]
    notes: str = ""


class TestOut(BaseModel):
    __test__ = False  # not a pytest test class
    id: UUID
    plan_id: UUID
    type: str
    scheduled_for: datetime | None
    executed_at: datetime | None
    executed_by: UUID | None
    outcome: str
    rto_met: bool | None
    rpo_met: bool | None
    evidence: dict[str, Any]
    notes: str
    created_at: datetime


class StepUpsertRequest(BaseModel):
    ordinal: int
    description: str
    result: Literal["pending", "pass", "fail"] = "pending"
    evidence_ref: str | None = None


class StepOut(BaseModel):
    id: UUID
    test_id: UUID
    ordinal: int
    description: str
    result: str
    evidence_ref: str | None


class FindingCreateRequest(BaseModel):
    description: str
    severity: Literal["low", "medium", "high", "critical"]


class FindingUpdateRequest(BaseModel):
    remediation_state: Literal["open", "in_progress", "resolved"] | None = None
    change_ref: UUID | None = None
    description: str | None = None


class FindingOut(BaseModel):
    id: UUID
    test_id: UUID
    description: str
    severity: str
    remediation_state: str
    change_ref: UUID | None
    created_at: datetime
    resolved_at: datetime | None


class TestReportOut(BaseModel):
    __test__ = False  # not a pytest test class
    test: TestOut | None
    plan: PlanOut | None
    steps: list[StepOut]
    findings: list[FindingOut]
    evidence: dict[str, Any]


class PeriodReportOut(BaseModel):
    outcome_counts: dict[str, int]
    plans_tested: list[UUID]
    open_findings_by_severity: dict[str, int]
    review_overdue_plans: list[UUID]
