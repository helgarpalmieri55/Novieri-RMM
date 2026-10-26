"""Request approval and rejection (spec.md §4, §8.2, §8.3).

`create_ticket` (`tickets.py`, task 9's decision -- see that module's
docstring) is the ONLY place a `ticket_approval` row is ever inserted: a
request whose category has `requires_approval = True` gets one, in state
`pending`, in the same transaction that lands the ticket on
`pending_approval`. `decide_approval` below never creates one -- it loads
the existing row and stamps it, exactly the shape this task's brief
describes. `uq_ticket_approval_org_ticket` guarantees at most one row per
ticket at the database level; this module's job is to guarantee at most one
row is ever *decided* per ticket, which is the `state != "pending"` check
below, not a second creation path.

**The self-approval prohibition is the entire reason `ticket.approve` is
split from `ticket.write` (spec §8.2).** A requester who also holds
`ticket.approve` -- plausible for a small org's admin -- must still not be
able to sign off on their own request. This is checked explicitly here,
never left to fall out of some other guard, and is the single most
important behavior this file has (spec §4, §12.3).

**RBAC layering.** The base `ticket.approve` permission gate is a route-layer
concern (`require_permission("ticket.approve")`, the same pattern every other
module in this platform uses -- see `budget/api.py`) and is out of scope for
this file, which has no route yet. What IS this file's job, because it is
data-driven rather than a static permission, is spec §4's second clause:
"when `ticket_category.approver_role_id` is set", the deciding actor must
also hold *that specific role* -- not just the `ticket.approve` permission in
the abstract. A `NULL` `approver_role_id` means any `ticket.approve` holder
may decide, so no membership check runs at all in that case.

Order of checks, all BEFORE anything is mutated, so an illegal call leaves
the ticket, the approval row, and the audit log completely untouched:

1. The ticket exists (org-scoped, via `tickets.get_ticket`).
2. It is a `request` -- incidents are never approved (spec §0.4, §4).
3. Its `ticket_approval` row, if one exists, is still `pending` -- a second
   decision on an already-decided ticket raises, rather than silently
   overwriting who decided it and when. The ROW is authoritative here, NOT
   the category's live `requires_approval` flag: once a ticket has actually
   entered the approval workflow, an unrelated later edit that flips the
   category's flag off must not stop that ticket from ever reaching a legal
   next status. `refdata.update_category` places no restriction on flipping
   `requires_approval` while tickets sit `pending_approval` under it, and
   `lifecycle.py` gives `pending_approval` no path forward except through
   this function -- consulting the category's CURRENT flag before the row
   exists would strand every such ticket permanently.
4. Only when NO row exists at all does the category's `requires_approval`
   flag get consulted, to decide which of two distinct errors applies:
   `False` means this category simply has no approval workflow; `True` with
   no row is the "invariant violated upstream" case `_load_approval`
   documents -- `create_ticket` is the only place a row is ever inserted.
5. The actor is not the ticket's own requester (the self-approval rule
   above).
6. If the category names an `approver_role_id`, the actor holds it.
7. A rejection carries a non-empty `reason`; approval does not require one.

Approval moves the ticket `pending_approval -> in_progress`; rejection moves
it to `rejected`, which `lifecycle.py` marks `FINAL_STATUSES` -- terminal,
no exceptions. Both moves go through `transitions.change_status`, never a
direct `ticket.status = ...` assignment, so `ticket_status_history` and the
`resolved_at`/`closed_at` stamping rules stay the single source of truth
they already are for every other status change in this module -- this file
adds no second way to move a ticket's status.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.events import publish
from app.core.i18n import tr
from app.core.ticket_words import type_quoted
from app.models import TicketApproval, UserRole
from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.refdata import resolve_category
from app.modules.service_desk.tickets import get_ticket
from app.modules.service_desk.transitions import change_status

_DECISION_TO_STATUS: dict[bool, str] = {True: "in_progress", False: "rejected"}
_DECISION_TO_AUDIT_ACTION: dict[bool, str] = {True: "ticket.approved", False: "ticket.rejected"}


async def _load_approval(
    session: AsyncSession, org_id: UUID, ticket_id: UUID
) -> TicketApproval | None:
    """Org-scoped load of the ticket's `ticket_approval` row, in WHATEVER
    state it is currently in -- created only by `tickets.create_ticket`
    (module docstring), never here. Returns `None` when no row exists;
    unlike an earlier revision, this does NOT raise on that -- `decide_approval`
    is the one that decides what a missing row means, since the right
    response (no approval workflow at all vs. an upstream invariant
    violation) depends on the category's `requires_approval` flag, which
    this function has no business knowing about."""
    return (
        await session.execute(
            select(TicketApproval).where(
                TicketApproval.org_id == org_id, TicketApproval.ticket_id == ticket_id
            )
        )
    ).scalar_one_or_none()


async def _actor_holds_role(
    session: AsyncSession, org_id: UUID, actor_id: UUID, role_id: UUID
) -> bool:
    """Whether `actor_id` has a `user_roles` row for `role_id` in this org --
    org- and device-group-scoped grants both count; spec §4 asks only whether
    the actor holds the named role at all, not whether the grant is
    org-wide."""
    membership = (
        await session.execute(
            select(UserRole.id).where(
                UserRole.org_id == org_id,
                UserRole.user_id == actor_id,
                UserRole.role_id == role_id,
            )
        )
    ).scalar_one_or_none()
    return membership is not None


async def decide_approval(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    ticket_id: UUID,
    *,
    approve: bool,
    reason: str = "",
) -> TicketApproval:
    """Approve or reject a pending service request (spec §4). Raises
    `TicketValidationError` (or its `TicketNotFoundError` subclass for the
    ticket lookup) and leaves the ticket, the approval row, and the audit
    log entirely untouched for every rejected precondition in the module
    docstring's numbered list -- nothing is mutated until every check has
    passed."""
    ticket = await get_ticket(session, org_id, ticket_id)

    if ticket.type != "request":
        raise TicketValidationError(
            tr(
                "ticket {number} is type={type}; only requests are approved",
                number=ticket.number,
                type=type_quoted(ticket.type),
            )
        )

    category = await resolve_category(session, org_id, ticket.category_id)

    # The row, if it exists, is authoritative (module docstring's #3/#4,
    # T1's fix) -- consulted BEFORE the category's live `requires_approval`
    # flag, precisely so that flipping that flag after the ticket already
    # entered the approval workflow cannot strand it.
    approval = await _load_approval(session, org_id, ticket.id)
    if approval is None:
        # No row was ever created for this ticket. Only now does the
        # category's CURRENT flag decide which error applies.
        if not category.requires_approval:
            raise TicketValidationError(
                tr(
                    "ticket {number}'s category {key} does not require approval",
                    number=ticket.number,
                    key=repr(category.key),
                )
            )
        raise TicketValidationError(
            tr(
                "ticket {number} has no ticket_approval row",
                number=ticket.number,
            )
        )

    if approval.state != "pending":
        # The decided state agrees with "aprobación" (feminine) in Spanish, while the
        # same words elsewhere agree with "ticket" (masculine), so each known state
        # has its own template; any other state is printed as it is.
        if approval.state == "approved":
            decided = tr(
                "ticket {number}'s approval was already decided: approved", number=ticket.number
            )
        elif approval.state == "rejected":
            decided = tr(
                "ticket {number}'s approval was already decided: rejected", number=ticket.number
            )
        else:
            decided = tr(
                "ticket {number}'s approval was already decided: {state}",
                number=ticket.number,
                state=approval.state,
            )
        raise TicketValidationError(decided)

    # Spec §4, module docstring's #1 concern: checked explicitly, regardless
    # of what permissions the actor holds.
    if actor_id == ticket.requester_id:
        raise TicketValidationError(tr("a requester may not approve their own request"))

    if category.approver_role_id is not None and not await _actor_holds_role(
        session, org_id, actor_id, category.approver_role_id
    ):
        raise TicketValidationError(
            tr("actor does not hold the role required to decide this ticket's approval")
        )

    if not approve and not reason.strip():
        raise TicketValidationError(tr("rejecting a request requires a non-empty reason"))

    to_status = _DECISION_TO_STATUS[approve]

    # The sole status-mutation path (module docstring) -- writes
    # `ticket_status_history` and `ticket.status_changed` itself, and raises
    # before any mutation if `pending_approval -> to_status` is somehow not
    # legal (it always is here, since `create_ticket` is the only path onto
    # `pending_approval` and this function is the only path off of it).
    #
    # `_approval_decision=True` (A1 fix, whole-branch security review):
    # `change_status` refuses `pending_approval -> in_progress`/`rejected`
    # from any ordinary caller, including the generic `/status` route, even
    # though `lifecycle.py` says both are legal -- that keyword is this
    # function's alone to pass, and only reached here AFTER every check above
    # (ticket.approve is the route's job; self-approval, approver_role_id
    # membership, and the rejection-reason requirement are this function's,
    # all already run by this point).
    await change_status(
        session,
        org_id,
        ticket.id,
        to_status=to_status,
        actor_id=actor_id,
        reason=reason,
        _approval_decision=True,
    )

    approval.state = "approved" if approve else "rejected"
    approval.decided_by = actor_id
    approval.decided_at = datetime.now(UTC)
    approval.reason = reason
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=_DECISION_TO_AUDIT_ACTION[approve],
        target_type="ticket",
        target_id=str(ticket.id),
        # Identifiers only -- never the free-text `reason` (spec §8.3: audit
        # metadata carries identifiers and state, never comment bodies; the
        # same convention `change_mgmt.decide_change` follows for its own
        # `comment`).
        metadata={"approval_id": str(approval.id)},
    )
    await session.flush()

    # Notification outbox (docs/modules/notification/spec.md §3, §6.4).
    # `change_status` above already published its own `ticket.status_changed`
    # event for the underlying transition -- this is a SECOND, distinct
    # event describing the approval decision itself, so a notification rule
    # can target "a request I approve was decided" without matching every
    # unrelated status change. `ticket.approval_decided` (not
    # `ticket.approval_requested` -- that event fires when a ticket ENTERS
    # `pending_approval`, i.e. when approval is NEEDED, not when it is
    # decided; see `tickets.create_ticket`/`transitions.change_status` and
    # `app.core.events`'s registry). Never the free-text `reason` (same
    # convention the audit metadata above follows) -- only the decision
    # outcome and routing-only identifiers. Payload shape matches
    # `notification.routing.resolve_recipients`'s fixed contract: every id
    # `str(uuid)` or `None`, `requester_id` present unconditionally.
    await publish(
        session,
        org_id=org_id,
        event_type="ticket.approval_decided",
        subject_type="ticket",
        subject_id=ticket.id,
        payload={
            "ticket_number": ticket.number,
            "subject": ticket.subject,
            "decision": "approved" if approve else "rejected",
            "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
            "assignee_id": str(ticket.assigned_to) if ticket.assigned_to else None,
            "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
        },
        actor_user_id=actor_id,
        client_id=None,  # plan 2026-10-26 Task 2 sets the ticket's client
    )
    return approval
