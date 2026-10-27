"""API-layer request/response models for the change management module's
HTTP routes (docs/modules/change_management/spec.md).

These are plain API-layer request/response shapes, not wire contracts —
this module dispatches no agent jobs, so there is no signed-job params/
result pair here and no `extra="forbid"` base is needed. Per security
invariant #3, no model in this file carries a secret value: change
management has no credential fields of its own.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class ChangeCreateRequest(BaseModel):
    # The client the change is for (MSP clients plan 2026-10-26 Task 7): it
    # must use change management. Fixed for the change's life -- no update
    # carries it.
    client_id: UUID
    type: Literal["standard", "normal", "emergency"]
    title: str
    description: str
    risk: Literal["low", "medium", "high"]
    affected: list[dict[str, Any]] = []
    window_start: datetime | None = None
    window_end: datetime | None = None
    rollback_plan: str = ""


class ChangeUpdateRequest(BaseModel):
    type: Literal["standard", "normal", "emergency"] | None = None
    title: str | None = None
    description: str | None = None
    risk: Literal["low", "medium", "high"] | None = None
    affected: list[dict[str, Any]] | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    rollback_plan: str | None = None


class ChangeOut(BaseModel):
    id: UUID
    client_id: UUID
    type: str
    title: str
    description: str
    risk: str
    affected: list[dict[str, Any]]
    window_start: datetime | None
    window_end: datetime | None
    rollback_plan: str | None
    state: str
    created_by: UUID
    created_at: datetime
    #: "auto" for the evidence record written when a patch approval or
    #: deployment happened with no change and no gate.
    origin: str = "manual"


class ApprovalDecisionRequest(BaseModel):
    comment: str | None = None


class ApprovalOut(BaseModel):
    id: UUID
    approver_id: UUID
    decision: str
    comment: str | None
    is_retrospective: bool
    decided_at: datetime


class TransitionRequest(BaseModel):
    action: Literal["begin", "complete", "cancel", "revise"]


class LinkCreateRequest(BaseModel):
    action_type: Literal["patch_run", "deployment", "provision_run", "patch_approval"]
    action_id: UUID


class LinkOut(BaseModel):
    id: UUID
    action_type: str
    action_id: UUID
    linked_at: datetime
    linked_by: UUID


class CalendarEntryOut(BaseModel):
    change_id: UUID
    title: str
    type: str
    risk: str
    state: str
    window_start: datetime | None
    window_end: datetime | None


class CalendarConflictOut(BaseModel):
    change_a: UUID
    change_b: UUID


class CalendarOut(BaseModel):
    entries: list[CalendarEntryOut]
    conflicts: list[CalendarConflictOut]


class EvidenceOut(BaseModel):
    change: ChangeOut
    approvals: list[ApprovalOut]
    links: list[LinkOut]
    audit_refs: list[UUID]


class GateOut(BaseModel):
    """Patch approvals and/or deployments reaching this group (or every
    device, when `device_group_id` is null) need an approved change."""

    id: UUID
    device_group_id: UUID | None
    device_group_name: str | None
    applies_to_patches: bool
    applies_to_deployments: bool
    created_at: datetime


class GateSetRequest(BaseModel):
    device_group_id: UUID | None = None
    applies_to_patches: bool = True
    applies_to_deployments: bool = True
