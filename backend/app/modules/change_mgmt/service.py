"""Change service (docs/modules/change_management/spec.md): create/edit a
draft change and drive it through its lifecycle via the pure state machine.

`transition_change` is the module's single lifecycle-mutation path — it
org-scoped-loads the change, computes `has_retrospective_approval` (an
org-scoped EXISTS over `change_approval`), hands everything to
`app.modules.change_mgmt.state.next_state` (invariant: all transition
legality — including SoD and the emergency retrospective-approval gate —
lives in that one pure function, never re-derived here), mutates
`change.state`, audits the transition (invariant #4), and then calls the
*pinned* ticket-port call site for the resulting state: `submit` pushes a
new external ticket, a move to a terminal state (`completed`/`cancelled`)
closes it, and every other transition (`begin`/`revise`) pushes an update.
`approve`/`reject` decisions go through `decide_change` instead (Task 7):
it inserts a new, immutable `ChangeApproval` row for every decision (never
updates one in place), delegates SoD and legality checking to the same
`next_state` function, and records the emergency retrospective-approval
flag that `_has_retrospective_approval` later reads.

Every query in this module carries an explicit `org_id` predicate
(invariant #5) — a change id from another org is never reachable through
any function here.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import ChangeApproval, ChangeRequest
from app.modules.change_mgmt.state import (
    ChangeAction,
    ChangeNotFoundError,
    ChangeValidationError,
    next_state,
)
from app.modules.change_mgmt.ticketing import ChangeTicketPort

_TERMINAL_STATES = {"completed", "cancelled"}

# Fields `update_change` is allowed to apply, in the order the brief lists
# them. Only non-None values passed by the caller are actually written.
_EDITABLE_FIELDS = (
    "type_",
    "title",
    "description",
    "risk",
    "affected",
    "window_start",
    "window_end",
    "rollback_plan",
)
# `type_` is the keyword-arg name (avoids shadowing the builtin); the ORM
# column is `type`.
_FIELD_TO_COLUMN = {"type_": "type"}


async def _load_change(session: AsyncSession, org_id: UUID, change_id: UUID) -> ChangeRequest:
    change = (
        await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()
    if change is None:
        raise ChangeNotFoundError(tr("change {change_id} not found", change_id=change_id))
    return change


async def create_change(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    type_: str,
    title: str,
    description: str,
    risk: str,
    affected: list[Any],
    window_start: datetime | None,
    window_end: datetime | None,
    rollback_plan: str | None,
) -> ChangeRequest:
    """Create a new `draft` change and audit `change.created`."""
    change = ChangeRequest(
        org_id=org_id,
        type=type_,
        title=title,
        description=description,
        risk=risk,
        affected=affected,
        window_start=window_start,
        window_end=window_end,
        rollback_plan=rollback_plan,
        state="draft",
        created_by=actor_id,
    )
    session.add(change)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.created",
        target_type="change",
        target_id=str(change.id),
        metadata={"type": type_, "risk": risk, "title": title},
    )
    await session.flush()
    return change


async def update_change(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    change_id: UUID,
    **fields: Any,
) -> ChangeRequest:
    """Apply the given non-None fields to a `draft` change; any other state
    raises `ChangeValidationError` — only a draft is editable."""
    change = await _load_change(session, org_id, change_id)
    if change.state != "draft":
        raise ChangeValidationError(tr("only a draft change can be edited"))

    for field in _EDITABLE_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        column = _FIELD_TO_COLUMN.get(field, field)
        setattr(change, column, value)

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="change.updated",
        target_type="change",
        target_id=str(change.id),
        metadata={},
    )
    await session.flush()
    return change


async def _has_retrospective_approval(session: AsyncSession, org_id: UUID, change_id: UUID) -> bool:
    result = await session.execute(
        select(
            exists().where(
                ChangeApproval.org_id == org_id,
                ChangeApproval.change_id == change_id,
                ChangeApproval.decision == "approved",
                ChangeApproval.is_retrospective.is_(True),
            )
        )
    )
    return bool(result.scalar())


async def transition_change(
    session: AsyncSession,
    ticket: ChangeTicketPort,
    *,
    org_id: UUID,
    actor_id: UUID,
    change_id: UUID,
    action: ChangeAction,
) -> ChangeRequest:
    """Drive `change_id` through one lifecycle transition (`submit`/`begin`/
    `complete`/`cancel`/`revise` — `approve`/`reject` are Task 7), then call
    the pinned ticket-port site for the resulting state."""
    change = await _load_change(session, org_id, change_id)
    old_state = change.state

    has_retrospective_approval = await _has_retrospective_approval(session, org_id, change_id)

    new_state = next_state(
        current=change.state,
        action=action,
        type_=change.type,
        created_by=change.created_by,
        actor_id=actor_id,
        has_retrospective_approval=has_retrospective_approval,
    )
    change.state = new_state
    await session.flush()

    if action == "submit":
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="change.submitted",
            target_type="change",
            target_id=str(change.id),
            metadata={"from": old_state, "to": new_state, "action": action},
        )
    else:
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="change.transitioned",
            target_type="change",
            target_id=str(change.id),
            metadata={"from": old_state, "to": new_state, "action": action},
        )
    await session.flush()

    if action == "submit":
        await ticket.push_change(session, org_id=org_id, change=change)
    elif new_state in _TERMINAL_STATES:
        await ticket.close_change(session, org_id=org_id, external_id="")
    else:
        await ticket.update_change(session, org_id=org_id, external_id="", change=change)

    return change


_DECISION_TO_ACTION: dict[str, ChangeAction] = {"approved": "approve", "rejected": "reject"}
_DECISION_TO_AUDIT_ACTION: dict[str, str] = {
    "approved": "change.approved",
    "rejected": "change.rejected",
}


async def decide_change(
    session: AsyncSession,
    ticket: ChangeTicketPort,
    *,
    org_id: UUID,
    actor_id: UUID,
    change_id: UUID,
    decision: str,
    comment: str | None,
) -> ChangeApproval:
    """Record an approve/reject decision on `change_id`.

    SoD and transition legality are validated entirely by
    `app.modules.change_mgmt.state.next_state` -- this function never
    re-derives them, it just calls that pure function and lets a raised
    `ChangeValidationError` propagate (e.g. the actor being the change's own
    requester, or deciding a change in an illegal state).

    Every decision is inserted as a brand-new `ChangeApproval` row; an
    existing row for the same change is never updated or revoked in place,
    so the full decision history persists across resubmissions. An emergency
    change's retrospective decision (recorded while the change is already
    `implementing`) does not move the run state -- `next_state` returns the
    unchanged `current` state for that case -- but the inserted row is what a
    later `transition_change(complete)` checks for via
    `_has_retrospective_approval`.
    """
    change = await _load_change(session, org_id, change_id)

    if decision not in _DECISION_TO_ACTION:
        raise ChangeValidationError(tr("invalid decision: {decision}", decision=repr(decision)))
    action = _DECISION_TO_ACTION[decision]

    is_retrospective = change.type == "emergency" and change.state == "implementing"

    new_state = next_state(
        current=change.state,
        action=action,
        type_=change.type,
        created_by=change.created_by,
        actor_id=actor_id,
        has_retrospective_approval=False,
    )

    approval = ChangeApproval(
        org_id=org_id,
        change_id=change_id,
        approver_id=actor_id,
        decision=decision,
        comment=comment,
        is_retrospective=is_retrospective,
    )
    session.add(approval)
    await session.flush()

    change.state = new_state
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=_DECISION_TO_AUDIT_ACTION[decision],
        target_type="change",
        target_id=str(change.id),
        metadata={"decision": decision, "is_retrospective": is_retrospective},
    )
    await session.flush()

    await ticket.update_change(session, org_id=org_id, external_id="", change=change)

    await session.flush()
    return approval


async def get_change(session: AsyncSession, org_id: UUID, change_id: UUID) -> ChangeRequest | None:
    return (
        await session.execute(
            select(ChangeRequest).where(
                ChangeRequest.org_id == org_id, ChangeRequest.id == change_id
            )
        )
    ).scalar_one_or_none()


async def list_changes(session: AsyncSession, org_id: UUID) -> list[ChangeRequest]:
    return list(
        (await session.execute(select(ChangeRequest).where(ChangeRequest.org_id == org_id)))
        .scalars()
        .all()
    )
