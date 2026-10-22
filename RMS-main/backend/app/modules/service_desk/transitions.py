"""Ticket status transitions and assignment (spec.md §3, §5, §8.3).

**`change_status` is the ONLY place a ticket's `status` column moves after
creation.** It defers entirely to `lifecycle.assert_transition` for legality --
this module does not re-derive, shortcut, or add a second guard on top of it.
Spec §3.3 names the failure mode directly: the Budget module produced two
Critical double-counts because two files each implemented "what state can this
be in" and disagreed. `lifecycle.py` is the sole authority; this module only
calls it.

**Never gate this path on `lifecycle.TERMINAL_STATUSES`.** `TERMINAL_STATUSES`
contains `closed`, which is deliberately reopenable (`closed -> open` for an
incident, `closed -> in_progress` for a request, spec §3.1/§3.2). A `if status
in TERMINAL_STATUSES: reject()` guard here would silently make every
legitimate reopen impossible -- the ticket would be closed forever with no
error anyone could act on. `assert_transition` already knows which reopens are
legal and which terminal states (`FINAL_STATUSES`: `cancelled`, `rejected`) are
truly final; that is the only check this module needs or performs.

**`resolved_at`/`closed_at` treat `fulfilled` as the request lifecycle's
counterpart to `resolved`.** Both columns are generic on `ticket`, not
per-type, and spec §3.3 itself pairs the two ("the whole reason `closed` is
distinct from `resolved`/`fulfilled`") -- so entering either `resolved`
(incident) or `fulfilled` (request) stamps `resolved_at`, and entering `closed`
(either type) stamps `closed_at`. A plain forward progression (`resolved`/
`fulfilled` -> `closed`) or a cancellation (`resolved`/`fulfilled` ->
`cancelled`) is not a reopen and leaves the earlier stamp alone as the
historical record that the ticket really was resolved/fulfilled at that time.

**Any reopen clears `resolved_at`** (spec §3.3, task 8's S2 fix) -- reopening
means leaving `resolved`/`fulfilled` for an active status (not `closed`, not
`cancelled`), or leaving `closed` at all, since `closed`'s only legal exits ARE
the reopen edges. Both shapes of reopen clear `resolved_at`, with no
exception: an earlier revision cleared it only for the first shape and left it
set for `closed -> open`/`closed -> in_progress`, which meant two tickets,
reopened the same way, disagreed on whether `resolved_at IS NOT NULL` -- the
exact column the SLA sub-project reads to mean "this ticket's open work
concluded". `closed_at` follows the narrower rule it always did: stamped on
entering `closed`, cleared on leaving it (which, again, is always a reopen).

**Every transition records an actor** (spec §3.3's last bullet). `actor_type`
is restricted to `"user"`/`"system"` and cross-checked against `actor_id`
being non-`None`/`None` respectively -- the same pairing invariant
`ck_ticket_human_attribution`/`ck_comment_author` enforce elsewhere in this
module, here as a typed check rather than a DB constraint, since
`ticket_status_history` carries no such CHECK. A machine transition (Phase 15
alert recovery, future auto-close) uses `actor_type="system"`, `actor_id=None`,
so it never reads as a technician's action; `change_status` rejects a caller
that gets the pairing backwards rather than silently writing a corrupt
history row.

**`_APPROVAL_ONLY_EDGES` is an AUTHORIZATION layer on top of `lifecycle.py`,
never a second transition table (whole-branch security review, finding A1).**
`lifecycle.py` says `pending_approval -> in_progress` and `pending_approval ->
rejected` are legal `request` transitions, and that remains completely true
and completely unchanged here -- `assert_transition` still decides legality
and nothing in this module re-derives or narrows it. What this module adds is
WHO may drive those two specific edges: only `approvals.decide_approval`,
via the private `_approval_decision=True` keyword it alone passes. A caller
reaching `change_status` any other way -- concretely, the generic `POST
/tickets/{id}/status` route, gated on `ticket.write` ALONE -- gets a
`TicketValidationError` on exactly those two edges, even though
`can_transition` returns `True` for both. Without this, a technician holding
only `ticket.write` (correctly 403'd from the dedicated `/approve` route)
could file a request needing approval with themselves as requester and then
self-approve or self-reject it through `/status`, skipping `ticket.approve`
(invariant #6), the self-approval prohibition, `approver_role_id` membership,
the mandatory rejection reason, and both `ticket.approved`/`ticket.rejected`
audit events -- while leaving `ticket_approval` permanently `state='pending'`,
`decided_by=NULL`, so spec §8.3's audit trail could never answer "who
approved this?" `pending_approval -> cancelled` is deliberately NOT in this
set: withdrawing your own request is a legitimate `ticket.write` action and
stays reachable through the generic path.

**`assigned_to`/`queue_id` are NOT in `tickets.TICKET_EDITABLE_FIELDS`.**
Spec §5 requires both to need `ticket.assign`, and §8.3 files the change under
`ticket.assigned` -- not `ticket.updated`. Leaving them in the generic
`update_ticket` PATCH path (gated on `ticket.write`) would make the
`ticket.assign` permission decorative and misfile assignment history under
the wrong audit action. `assign_ticket` is their only write path AFTER
CREATION -- `tickets.create_ticket` still writes `assigned_to` itself, a
narrow, deliberate carve-out (task 8's S6) -- and a caller that includes
either key in a plain `PATCH` is REFUSED and pointed here by name, identical
to how `status` is refused and pointed at `change_status` (SLA task 8 review
F1; `tickets._reject_non_editable_fields`). That refusal is why
`assign_ticket` now carries the keyword-only `actor_type` contract too: SLA
§6's queue-move escalation is system-originated, this is the service it must
call, and it must not have to invent a human to satisfy the audit row.

**`assign_ticket` takes `fields: dict[str, Any]`, the same PRESENCE
convention `tickets.update_ticket` uses (task 8's S1 fix).** A key's
PRESENCE, not its value, decides whether that field is touched -- the route
layer passes `schema.TicketAssign.model_dump(exclude_unset=True)`. An earlier
revision gave `assigned_to` a required keyword with no default, on the
reasoning that `None` is itself a legitimate target (unassign) so there was no
unambiguous "leave alone" sentinel for it -- but that made `schema.TicketAssign`'s
own documented contract ("a caller can move the queue without touching the
assignee") unreachable: a caller either omits `assigned_to` and gets a
`TypeError`, or supplies the ticket's current value defensively, and a caller
following the *documented* convention (bare presence) instead unassigns
whoever held the ticket. Presence fixes this the same way it already fixes
`update_ticket`: an omitted `assigned_to` leaves the current assignee
untouched; an explicit `assigned_to: None` unassigns; a supplied id is
resolved org-scoped. `queue_id` follows the identical rule -- omitted leaves
the queue untouched; since `ticket.queue_id` is NOT NULL, an explicit `null`
is rejected up front as a typed 400 rather than treated as "no change" (that
would silently accept a request that cannot mean what it says).

`assign_ticket` does **not** gate on `TERMINAL_STATUSES` (or any status at
all) -- reassigning ownership or moving the queue of a closed ticket is an
administrative bookkeeping action, not a content edit, and the brief's
interface contract for this function names only `lifecycle.assert_transition`
and `tickets.get_ticket` as consumed, not the terminal-status set. This is a
deliberate asymmetry with `tickets.update_ticket`'s field-edit gate, not an
oversight, and it is now spec-pinned rather than living only in this comment
and a commit message (spec §5, task 8's S3 fix): §3.3's `closed` "accepts no
field edits and no new comments" describes `update_ticket`'s PATCH path, not
this administrative-bookkeeping operation, so assignment is exempt from the
`closed` edit freeze on purpose.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.events import publish
from app.core.i18n import tr
from app.core.ticket_words import status_quoted, status_word
from app.models import Ticket, TicketStatusHistory
from app.modules.service_desk.errors import TicketValidationError
from app.modules.service_desk.lifecycle import FINAL_STATUSES, assert_transition
from app.modules.service_desk.refdata import resolve_queue
from app.modules.service_desk.tickets import get_ticket, resolve_user
from app.modules.sla.tracking import (
    PAUSING_STATUSES,
    note_cancelled,
    note_resolved,
    pause,
    recompute_for_retriage,
    resume,
    sla_misconfiguration_is_not_fatal,
)

# The request lifecycle's counterpart to incident's `resolved` -- see module
# docstring. Kept local to this file: it is timestamp-stamping metadata, not
# a second copy of the transition table (`lifecycle.py` remains the sole
# authority on legality).
_RESOLVED_LIKE_STATUSES: frozenset[str] = frozenset({"resolved", "fulfilled"})

# Progressing onward from resolved/fulfilled, or finalizing via cancellation,
# is not a reopen and must not clear `resolved_at`. See module docstring's
# stamp-clearing walkthrough.
_NOT_A_REOPEN_TARGET: frozenset[str] = frozenset({"closed", "cancelled"})

# A1 fix (module docstring's new note): both edges are LEGAL per
# `lifecycle.py` -- this is not a second transition table, it does not
# change what `can_transition`/`assert_transition` say. It restricts WHO may
# drive them through `change_status` to callers that pass
# `_approval_decision=True`, i.e. only `approvals.decide_approval`.
_APPROVAL_ONLY_EDGES: frozenset[tuple[str, str]] = frozenset(
    {("pending_approval", "in_progress"), ("pending_approval", "rejected")}
)


# ---------------------------------------------------------------------------
# change_status
# ---------------------------------------------------------------------------


async def change_status(
    session: AsyncSession,
    org_id: UUID,
    ticket_id: UUID,
    *,
    to_status: str,
    actor_id: UUID | None,
    actor_type: Literal["user", "system"] = "user",
    reason: str = "",
    _approval_decision: bool = False,
) -> Ticket:
    """Move `ticket_id` from its current status to `to_status` (spec §3, §5's
    reopen edges included), org-scoped-loaded via `tickets.get_ticket`.

    Legality is decided ENTIRELY by `lifecycle.assert_transition` -- this
    function performs no other check on which moves are allowed and never
    gates on `TERMINAL_STATUSES` (module docstring). An illegal transition
    raises `TicketValidationError` before anything is mutated, so the ticket
    is left completely unchanged.

    `_approval_decision` (module docstring's A1 fix, private -- leading
    underscore, no public caller should set it) gates the two edges in
    `_APPROVAL_ONLY_EDGES`: `False` (the default every ordinary caller,
    including the generic `/status` route, gets) refuses them with a
    `TicketValidationError` even though `lifecycle.py` says they are legal.
    Only `approvals.decide_approval` passes `True`, after it has already run
    every approval-specific check (`ticket.approve`, self-approval,
    `approver_role_id` membership, the rejection-reason requirement).

    Writes exactly one `ticket_status_history` row per call, records the
    audit event `ticket.status_changed`, and stamps/clears `resolved_at`/
    `closed_at` per the module docstring's rules.
    """
    if actor_type not in ("user", "system"):
        raise TicketValidationError(
            tr(
                "unknown actor_type: {actor_type}",
                actor_type=repr(actor_type),
            )
        )
    if actor_type == "system" and actor_id is not None:
        raise TicketValidationError(
            tr("actor_type='system' transitions must not carry an actor_id")
        )
    if actor_type == "user" and actor_id is None:
        raise TicketValidationError(tr("actor_type='user' transitions require an actor_id"))

    ticket = await get_ticket(session, org_id, ticket_id)
    from_status = ticket.status

    # Sole authority on legality (module docstring) -- raises before any
    # mutation below runs. `lifecycle.py` is unmodified and unconsulted about
    # WHO may drive a transition; that is the separate check right below.
    assert_transition(ticket.type, from_status, to_status)

    # A1 fix (module docstring): these two edges are legal per the line
    # above, but reserved for `approvals.decide_approval` alone. Checked
    # AFTER `assert_transition` (an unknown/illegal transition still raises
    # its own error first, unaffected by this) and BEFORE anything is
    # mutated, so a rejected call here leaves the ticket exactly as
    # untouched as any other illegal transition does.
    if (from_status, to_status) in _APPROVAL_ONLY_EDGES and not _approval_decision:
        raise TicketValidationError(
            tr(
                "{from_status} -> {to_status} may only be driven by an approval decision "
                "(ticket.approve), not the generic status endpoint",
                from_status=status_quoted(from_status),
                to_status=status_quoted(to_status),
            )
        )

    now = datetime.now(UTC)

    # `closed`'s only legal exits are the reopen edges (its outgoing set is
    # exactly {"open"} / {"in_progress"} in lifecycle.py), so `from_status ==
    # "closed"` always means a reopen here -- unconditional, no further check
    # needed.
    is_reopen = (
        from_status in _RESOLVED_LIKE_STATUSES and to_status not in _NOT_A_REOPEN_TARGET
    ) or from_status == "closed"

    if to_status in _RESOLVED_LIKE_STATUSES:
        ticket.resolved_at = now
    elif is_reopen:
        # Any reopen clears resolved_at (module docstring's S2 fix) --
        # leaving resolved/fulfilled for an active status, or leaving closed
        # at all, is a reopen either way, and both must erase the same stamp.
        ticket.resolved_at = None

    if to_status == "closed":
        ticket.closed_at = now
    elif from_status == "closed":
        ticket.closed_at = None

    ticket.status = to_status
    await session.flush()

    session.add(
        TicketStatusHistory(
            org_id=org_id,
            ticket_id=ticket.id,
            from_status=from_status,
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
    )
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="ticket.status_changed",
        target_type="ticket",
        target_id=str(ticket.id),
        metadata={"from_status": from_status, "to_status": to_status},
    )
    await session.flush()

    # ------------------------------------------------------------------
    # SLA clock control (docs/modules/sla/spec.md §5, §6.0). Both branches
    # key off `sla.tracking.PAUSING_STATUSES` -- imported, never restated as
    # two string literals here, so a status added to that set on the SLA
    # module's side reaches this wiring automatically (§1: this module owns
    # no part of the SLA's definition of "not on the clock").
    # ------------------------------------------------------------------
    #
    # Written as two independent tests rather than if/elif because they ARE
    # independent facts: `pending` is incident-only and `pending_approval` is
    # request-only (`ck_ticket_status_matches_type`), so no legal transition
    # can both enter and leave a pausing status, and an implementation that
    # relied on `elif` to make that true would be relying on the transition
    # table rather than saying it.
    #
    # ORDER: RESUME RUNS BEFORE `note_resolved` (bottom of this function), and
    # that ordering is behaviour, not tidiness. `pending -> resolved` is a
    # legal edge the ticket core added on purpose ("the third party comes back
    # and the fix lands directly from there", `lifecycle.py`). Resuming first
    # credits the pause to the still-running deadlines and only then does
    # `note_resolved` stop the clocks, so a ticket that was blocked for three
    # days and fixed the moment the third party replied reads as MET against
    # its extended deadline. Stopping the clocks first would have `resume`'s
    # running-clock guard decline to re-derive anything, and the same ticket
    # would read as resolved three days past a deadline it was never actually
    # owed -- the pause credit silently discarded at the only moment it
    # mattered.
    if to_status in PAUSING_STATUSES:
        async with sla_misconfiguration_is_not_fatal("pause", ticket.id):
            await pause(session, org_id, ticket.id)
    if from_status in PAUSING_STATUSES:
        async with sla_misconfiguration_is_not_fatal("resume", ticket.id):
            await resume(session, org_id, ticket.id)

    # Void the promise on a terminal-not-resolved status (§6.0, migration
    # 0038). `FINAL_STATUSES` (`cancelled`/`rejected`), NOT
    # `TERMINAL_STATUSES`: `closed` is in the wider set and is deliberately
    # reopenable, and a closed ticket's promise was DISCHARGED (it passed
    # through `resolved`/`fulfilled`, where `note_resolved` stopped both
    # clocks), not voided. Voiding on `closed` would additionally void a
    # ticket that could legally reopen, leaving a reopened ticket with a dead
    # promise and no path back.
    #
    # Deliberately NOT paired with a met stamp: cancelling must never claim
    # the promise was kept (see `note_cancelled`). Runs after `resume` above
    # so a ticket cancelled straight out of `pending` closes its pause
    # honestly before the promise is voided.
    if to_status in FINAL_STATUSES:
        async with sla_misconfiguration_is_not_fatal("note_cancelled", ticket.id):
            await note_cancelled(session, org_id, ticket.id)

    # Notification outbox (docs/modules/notification/spec.md §3, §6.4). Same
    # transaction as the mutation above -- `publish()` neither commits nor
    # opens its own transaction, so this event is atomic with `ticket.status`
    # actually having moved. Payload keys match `notification.routing.
    # resolve_recipients`'s fixed contract exactly: every id a `str(uuid)` or
    # `None`, `requester_id` present unconditionally.
    await publish(
        session,
        org_id=org_id,
        event_type="ticket.status_changed",
        subject_type="ticket",
        subject_id=ticket.id,
        payload={
            "ticket_number": ticket.number,
            "subject": ticket.subject,
            "from_status": from_status,
            "to_status": to_status,
            "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
            "assignee_id": str(ticket.assigned_to) if ticket.assigned_to else None,
            "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
        },
        actor_user_id=actor_id,
    )

    # A second, distinct notification for the one edge that lands a ticket
    # ON `pending_approval` (`lifecycle.py`'s request `new -> pending_approval`
    # edge -- `tickets.create_ticket` is the other, far more common entry to
    # that same state, and publishes this identically-typed event itself for
    # that path). Guarded on `to_status` alone -- a transition can only ever
    # be entering `pending_approval` here, never re-entering it, since
    # `lifecycle.py` gives that state no incoming self-edge and `_TRANSITIONS`
    # never lists it as its own destination from itself -- so this fires at
    # most once per call, same as the `ticket.status_changed` publish above.
    # Same payload contract as `create_ticket`'s: every id `str(uuid)` or
    # `None`, `requester_id` present unconditionally.
    if to_status == "pending_approval":
        await publish(
            session,
            org_id=org_id,
            event_type="ticket.approval_requested",
            subject_type="ticket",
            subject_id=ticket.id,
            payload={
                "ticket_number": ticket.number,
                "subject": ticket.subject,
                "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
                "assignee_id": str(ticket.assigned_to) if ticket.assigned_to else None,
                "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
            },
            actor_user_id=actor_id,
        )

    # `ticket.resolved` (whole-branch review finding C2). Registered in
    # `app.core.events.EVENT_TYPES` and rule-selectable since Task 7, but
    # nothing published it: spec §0.4 calls telling a requester their ticket
    # was resolved "most of the product value", and it silently never fired.
    # Fires on BOTH `resolved`-like statuses -- `to_status in
    # _RESOLVED_LIKE_STATUSES` is exactly the incident `resolved` / request
    # `fulfilled` pairing this file already treats as one concept for
    # `resolved_at` stamping (module docstring) -- there is a single
    # `ticket.resolved` event type registered (not a second `ticket.fulfilled`
    # type), so a request reaching `fulfilled` publishes the SAME event a
    # resolved incident does; a rule on `ticket.resolved` routed to the
    # requester notifies them either way, which is the point. Guarded on
    # `to_status` alone, matching the `pending_approval` block above: a
    # ticket can only be ENTERING a resolved-like status here (leaving one is
    # `is_reopen`, a different `to_status` entirely), so this fires at most
    # once per call.
    if to_status in _RESOLVED_LIKE_STATUSES:
        await publish(
            session,
            org_id=org_id,
            event_type="ticket.resolved",
            subject_type="ticket",
            subject_id=ticket.id,
            payload={
                "ticket_number": ticket.number,
                "subject": ticket.subject,
                "queue_id": str(ticket.queue_id) if ticket.queue_id else None,
                "assignee_id": str(ticket.assigned_to) if ticket.assigned_to else None,
                "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
            },
            actor_user_id=actor_id,
        )
        # SLA met detection (docs/modules/sla/spec.md §0.6), on the same
        # `to_status in _RESOLVED_LIKE_STATUSES` test that stamped
        # `resolved_at` above and published `ticket.resolved` here -- so an
        # incident reaching `resolved` and a request reaching `fulfilled` are
        # one concept for the SLA exactly as they already are for this file.
        # Stops the resolution clock, and the response clock with it: §0.6 is
        # explicit that a ticket resolved outright with no reply (a duplicate,
        # say) cannot owe a response forever. No-op for a ticket with no
        # `ticket_sla` row (SLA spec §3). Same transaction as the status move.
        await note_resolved(session, org_id, ticket.id)
    return ticket


# ---------------------------------------------------------------------------
# assign_ticket
# ---------------------------------------------------------------------------


async def assign_ticket(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    ticket_id: UUID,
    fields: dict[str, Any],
    *,
    actor_type: Literal["user", "system"] = "user",
) -> Ticket:
    """Assign, reassign, unassign, and/or move queue (spec §5) -- the only
    write path for `assigned_to`/`queue_id` AFTER CREATION now that both are
    absent from `tickets.TICKET_EDITABLE_FIELDS` (module docstring).

    `actor_type` mirrors `change_status`'s and `tickets.update_ticket`'s
    system-actor path exactly (SLA task 8 review, F1): keyword-only with a
    `"user"` default, so the `/assign` route and every existing test are
    unchanged, and the same three guards run before the ticket is loaded --
    an unknown `actor_type`, a `"system"` actor carrying an `actor_id`, and a
    `"user"` actor with `actor_id=None` are all typed rejections.

    It exists because this is the queue-move service, and SLA §6's
    queue-move escalation is system-originated with no human actor. Without
    it, escalation's only options were to invent a synthetic user for the
    `ticket.assigned` audit row -- putting a fake person in the immutable log
    (invariant #4) -- or to route the queue move back through
    `update_ticket`, which is exactly what F1 refused. `actor_id=None` also
    flows to `publish`, which documents that same allowance for
    machine-origin events.

    `fields` follows `tickets.update_ticket`'s presence convention exactly
    (module docstring's S1 fix): the route passes
    `schema.TicketAssign.model_dump(exclude_unset=True)`, and a key's
    PRESENCE, not its value, decides whether that field is touched. An
    omitted `assigned_to` leaves the current assignee untouched; an explicit
    `assigned_to: None` clears it; a supplied id is resolved org-scoped via
    `tickets.resolve_user`, raising `TicketNotFoundError` for a cross-org or
    nonexistent user (spec §8.1, invariant #5). An omitted `queue_id` leaves
    the queue untouched; `ticket.queue_id` is NOT NULL, so an explicit
    `queue_id: None` is rejected up front as a typed 400 rather than silently
    treated as "no change" -- a caller that types `null` there almost
    certainly means something else entirely, and update_ticket's own
    `NON_NULLABLE_TICKET_FIELDS` pass rejects the same shape of request the
    same way. Nothing is `setattr`'d on the tracked `Ticket` until both
    cross-references are resolved, matching `tickets.update_ticket`'s
    ordering discipline.
    """
    # Actor validation first, before anything is loaded -- the identical
    # three guards, in the identical order, that `change_status` and
    # `tickets.update_ticket` run.
    if actor_type not in ("user", "system"):
        raise TicketValidationError(
            tr(
                "unknown actor_type: {actor_type}",
                actor_type=repr(actor_type),
            )
        )
    if actor_type == "system" and actor_id is not None:
        raise TicketValidationError(
            tr("actor_type='system' assignments must not carry an actor_id")
        )
    if actor_type == "user" and actor_id is None:
        raise TicketValidationError(tr("actor_type='user' assignments require an actor_id"))

    # Reject null on the NOT NULL `queue_id` before the row is even loaded --
    # same ordering `update_ticket` uses for NON_NULLABLE_TICKET_FIELDS.
    if "queue_id" in fields and fields["queue_id"] is None:
        raise TicketValidationError(tr("queue_id cannot be null"))

    ticket = await get_ticket(session, org_id, ticket_id)
    # FINAL_STATUSES, not TERMINAL_STATUSES -- the one place in this module
    # where that distinction changes behavior rather than merely naming it.
    #
    # `cancelled` and `rejected` are dead: nothing leaves them, so handing one
    # to a technician is churn in the audit log for work that can never move.
    # `closed` is different -- it is reopenable, and reassigning ownership of a
    # closed ticket is administrative bookkeeping, not a content edit, so it
    # stays permitted (see `test_assign_ticket_permits_reassignment_of_a_closed_ticket`
    # and spec §5's assignment-exemption note, task 8's S3 fix).
    #
    # That asymmetry is why `update_ticket` gates on TERMINAL (a closed ticket
    # accepts no field edits) while assignment gates on FINAL.
    if ticket.status in FINAL_STATUSES:
        raise TicketValidationError(
            tr(
                "ticket {number} is {status} and cannot be assigned",
                number=ticket.number,
                status=status_word(ticket.status),
            )
        )

    resolved_assigned_to = ticket.assigned_to
    if "assigned_to" in fields:
        assigned_to = fields["assigned_to"]
        if assigned_to is not None:
            assignee = await resolve_user(session, org_id, assigned_to)
            resolved_assigned_to = assignee.id
        else:
            resolved_assigned_to = None

    # Captured BEFORE the write below, because it is what decides whether the
    # SLA re-triage hook at the bottom of this function fires: `resolve_policy`
    # keys on `(queue, priority)`, so a queue that did not actually move
    # changes no promise and must not restamp one.
    previous_queue_id = ticket.queue_id

    resolved_queue_id = ticket.queue_id
    if "queue_id" in fields:
        queue = await resolve_queue(session, org_id, fields["queue_id"])
        resolved_queue_id = queue.id

    ticket.assigned_to = resolved_assigned_to
    ticket.queue_id = resolved_queue_id
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="ticket.assigned",
        target_type="ticket",
        target_id=str(ticket.id),
        # Identifiers only, matching `ticket.created`/`ticket.updated`'s
        # metadata convention (spec §8.3) -- never subject/description bodies
        # (there are none here to leak, but the shape stays consistent).
        metadata={
            "assigned_to": str(resolved_assigned_to) if resolved_assigned_to is not None else None,
            "queue_id": str(resolved_queue_id),
        },
    )
    await session.flush()

    # Notification outbox (docs/modules/notification/spec.md §3, §6.4) --
    # same transaction, same reasoning as `change_status` above. Payload
    # shape matches `notification.routing.resolve_recipients`'s fixed
    # contract: every id a `str(uuid)` or `None`, `requester_id` present
    # unconditionally.
    await publish(
        session,
        org_id=org_id,
        event_type="ticket.assigned",
        subject_type="ticket",
        subject_id=ticket.id,
        payload={
            "ticket_number": ticket.number,
            "subject": ticket.subject,
            "queue_id": str(resolved_queue_id) if resolved_queue_id else None,
            "assignee_id": str(resolved_assigned_to) if resolved_assigned_to is not None else None,
            "requester_id": str(ticket.requester_id) if ticket.requester_id else None,
        },
        actor_user_id=actor_id,
    )

    # SLA re-triage, the QUEUE half of §3's "a priority or queue change with a
    # human actor" (the priority half lives in `tickets.update_ticket`). Two
    # call sites and not one because §6.0 forced the two escalation halves
    # through two different services; the hook has to follow the write.
    #
    # **`actor_type == "user"` IS §0.4'S LOOP GUARD** -- and the queue half is
    # the DANGEROUS one, which §6.0.1 says in as many words: re-resolving
    # after a system queue move would pick up the ESCALATION QUEUE's own
    # policy, typically a tighter one, recompute from `created_at`, breach
    # instantly and escalate again. That is §0.4's cascade arriving through a
    # second door, and `sla.breach._escalate` reaches this function with
    # `actor_type="system"` on exactly that path.
    #
    # Keyed on the queue having actually MOVED, not on `"queue_id" in fields`:
    # an `/assign` that only reassigns the technician, or that re-sends the
    # queue the ticket is already in, changes no `(queue, priority)` pair and
    # so changes no promise. Recomputing there would restamp `policy_source`
    # to `retriage` and re-derive deadlines for an assignment -- attributing a
    # promise to a triage decision nobody made.
    if actor_type == "user" and resolved_queue_id != previous_queue_id:
        async with sla_misconfiguration_is_not_fatal("recompute_for_retriage", ticket.id):
            await recompute_for_retriage(session, org_id, ticket)
    return ticket
