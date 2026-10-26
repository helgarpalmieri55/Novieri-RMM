"""Marking a promise missed, and escalating it exactly once
(docs/modules/sla/spec.md §0.4, §0.5, §2.5, §6, §6.0, §6.0.1, §8, §10.6,
§10.7; Task 9).

One entry point. `mark_breached` stamps the breached target's column,
publishes `ticket.sla_breached` to the transactional outbox, and -- when the
row's own policy enables it -- raises the ticket one step up
`PRIORITY_LADDER` and/or moves it to the policy's escalation queue. It sends
no mail: delivery belongs to the notification module (§0.5), which owns
channels, routing, retries and the egress guard.

THE CASCADE GUARD IS THE POINT OF THIS FILE (§0.4, §6.0.1). §0.3 says a
priority change recomputes both deadlines *from ticket creation*. Escalation
raises priority. Composed naively they loop, and the loop is not theoretical:
a `normal` ticket on an 8h target breaches at hour 8; escalation raises it to
`high` (4h); a recompute from creation makes it 8h old against a 4h target, so
it breaches again in the same second; that raises it to `urgent`; it breaches
again. It terminates only because `urgent` is the ceiling -- after three
escalations and three breach notifications inside one second, for one ticket.

So an escalation **never recomputes**. Not for the priority bump, and not for
the queue move. The queue move is the more dangerous half, because it looks
innocent: `resolve_policy` keys on `(queue, priority)` (§3), so re-resolving
after a queue move picks up the *escalation queue's own* policy -- usually a
tighter one, since that is what an escalation queue is for -- and the same
cascade runs through a second door with no priority change anywhere in it.
The guard here is structural rather than a flag: this module does not import
`recompute_for_retriage`, does not call `resolve_policy`, and never writes
`response_due_at` or `resolution_due_at`. `tracking._materialise` refuses
`policy_source='escalation'` at runtime from the other side, so the two
modules cannot both write the same row's attribution.

`ticket_sla.policy_id` AND `calendar_id` ARE LEFT ALONE, deliberately.
`tracking.resume`'s docstring records this as Task 9's choice to make: if
escalation repointed `policy_id` at the escalated priority's policy, then the
next `resume` or `recompute_for_retriage` -- both of which re-derive from
`ticket.created_at` under the row's stored policy -- would apply that shorter
target to the still-running clock, which is §0.4's cascade arriving later,
through an unrelated status change, with no escalation in sight. The row's
`policy_id` therefore keeps naming the policy that actually produced the
deadlines it holds, which is also what makes the breach event's `policy_id`
mean "the policy that was missed" rather than "the policy we just moved to".
`policy_source='escalation'` is then exactly what §0.4 says it is: the
attribution that says *why the deadlines on this row were not recomputed*.

WHAT THE EVENT PAYLOAD CARRIES, AND WHAT IT DELIBERATELY DOES NOT. The shape
is a fixed contract shared with the notification module -- `delivery.py`'s
`_render_ticket_sla_breached` reads `target`/`due_at`/`ticket_number`/
`subject`, `routing.resolve_recipients` reads `queue_id`, and
`test_notification_delivery.py` pins the whole wire format per event type, so
a drifted key fails that suite rather than this one:

    ticket_id, ticket_number, subject, target, due_at, policy_id, queue_id,
    priority

**There is no `requester_id` key, and there must never be one.** §6.1: the
breach mail is written staff-facing ("sign in to Toeshee to review it"), and
telling an external customer that their supplier missed a contractual deadline
is an outcome nobody chose. Since the Task 1 review that is belt *and* braces
-- `ck_rule_no_sla_breach_to_requester` and `STAFF_ONLY_EVENT_TYPES` refuse a
`recipient_kind='requester'` rule at write time -- but the omission stays,
because it is what makes the payload structurally unroutable to the requester
instead of merely policy-blocked.

THE EVENT DESCRIBES THE TICKET AS IT BREACHED, NOT AS ESCALATION LEFT IT. It
is published BEFORE any escalation is applied, so `queue_id` and `priority`
are the pre-escalation values. That is not incidental ordering:
`routing.resolve_recipients` matches rules on `payload["queue_id"]` and
expands `recipient_kind='queue_members'` from it, so publishing the
post-escalation queue would tell the *escalation* queue's members about a
breach they did not incur and leave the team that actually missed the deadline
never hearing about it. `policy_id` is pre-escalation for the same reason and
by the same token: it names the promise that was broken. The escalation itself
is not silent -- it writes its own `ticket.updated` / `ticket.assigned` /
`sla.escalated` rows (below).

TWO SERVICES, NOT ONE (§6.0). The priority bump goes through
`tickets.update_ticket` and the queue move through
`transitions.assign_ticket`, both `actor_type="system"` with `actor_id=None`.
This is not interchangeable: `queue_id` is absent from
`TICKET_EDITABLE_FIELDS` because service-desk §5 gates a queue move on
`ticket.assign` rather than `ticket.write` and files it under the
`ticket.assigned` audit action, and `update_ticket` now *raises* on it rather
than silently dropping it (which is what the Task 8 review found: the queue
half would have reported success while doing nothing). A combined escalation
is therefore two calls and two audit rows.

A TERMINAL TICKET IS NEVER ESCALATED -- NEITHER HALF (§6.0's closing note).
The two services do not agree on what "too late to touch" means:
`assign_ticket` refuses `FINAL_STATUSES` (`cancelled`/`rejected`), while
`update_ticket` refuses `TERMINAL_STATUSES` (those **plus `closed`**). On a
`closed` ticket a combined escalation would therefore move the queue
successfully and then raise on the priority bump -- a half-applied escalation
whose visible result depends on which of the two halves an admin happened to
configure. This module refuses the whole escalation for any status in
`TERMINAL_STATUSES`, which is the union of the two services' refusals and so
the only set on which both halves agree. Nothing partial is written, and
`escalated_at` stays NULL because nothing was escalated.

`closed` is the one `TERMINAL_STATUSES` member that still gets the breach
stamped and published: a missed promise on a ticket that later closed is real
history and staff-facing information, and suppressing it would quietly
under-report breaches. `cancelled`/`rejected` -- `FINAL_STATUSES` -- do NOT:
`mark_breached` refuses the stamp and the publish for those before `_escalate`
is even called, because `note_cancelled` has already declared the promise void
(§6.0's closing sentence; F1 of the sla-breach-wiring-review). THAT is the
primary guard, checked first on `tracking.voided_at` and then, for the rows
`voided_at` cannot see (migration 0038's deliberate non-backfill), on the
ticket's own `FINAL_STATUSES` membership -- see `mark_breached`'s docstring.
`TERMINAL_STATUSES` here in `_escalate` is a second, narrower-purposed guard
for a different failure mode (the two services half-applying an escalation),
not a second attempt at the void guard.

ONCE PER TICKET, NOT ONCE PER SWEEP (§2.5, §6, §10.7). `escalated_at` is the
latch, and it is on the `ticket_sla` row rather than derived from the breach
columns because the two clocks breach independently: a response breach that
escalated must not be escalated again when the resolution deadline passes an
hour later. Re-marking a target that is no longer breachable -- already
stamped `<target>_breached_at`, or stopped ON TIME -- is a silent no-op, so an
at-least-once sweep produces one stamp, one event and one escalation.

MET IS NOT THE SAME QUESTION AS MET ON TIME (§7). The guard here is
`tracking._response_breachable` / `_resolution_breachable`, NOT the
`_response_running` / `_resolution_running` pair the rest of this module's
neighbours use. A clock stopped AFTER its deadline (`met_at > due_at`) is not
running -- the reply really did happen and nothing may move that stamp -- but
the promise was still broken, so it must be stamped `<target>_breached_at` as
well. `met_at` records *when contact happened*; `breached_at` records *that
the promise was broken*; a late reply carries both, and neither column can be
derived from the other. Reading the running guard here was a shipped defect:
§7's sweep already selected such rows (`met_at IS NULL OR met_at > due_at`),
so each one was handed to this function and silently skipped every 300
seconds, forever, and SLA attainment measured whether the beat happened to
fire before or after the technician replied.

ATOMICITY IS THE CALLER'S TRANSACTION. Nothing here commits; every write is a
`flush` on the caller's session, so a failure anywhere (a cross-org escalation
queue, an off-ladder priority) leaves the caller free to roll the whole
`mark_breached` back rather than keeping a breach stamp whose escalation never
happened. Task 10's sweep owns per-ticket containment.

NOT THE SWEEP'S FILTERS. This function does not check `paused_at`, does not
compare `due_at` to now, and does not select rows -- deciding *which* rows have
actually breached is §7's job (Task 10). Handed a row, it marks it.

ORG SCOPING (invariant #5). `ticket_sla.ticket_id` and `policy_id` are plain
FKs with no org check, so a row one org owns can legally point at another org's
ticket or policy. Every load here is org-scoped, and all three predicates are
`tracking.py`'s -- imported rather than restated, because a tenancy predicate
copied into a second file is a tenancy predicate that can drift in one of them.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.events import publish
from app.core.i18n import tr
from app.modules.service_desk.lifecycle import FINAL_STATUSES, TERMINAL_STATUSES
from app.modules.service_desk.models import Ticket
from app.modules.service_desk.refdata import resolve_queue
from app.modules.service_desk.tickets import update_ticket
from app.modules.service_desk.transitions import assign_ticket
from app.modules.sla.errors import SlaValidationError
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import (
    _load_policy_in_org,
    _load_ticket_in_org,
    _load_tracking,
    _resolution_breachable,
    _response_breachable,
)

__all__ = ["PRIORITY_LADDER", "mark_breached"]

# Spec §6, in order. Must stay in agreement with `service_desk.schema
# .TicketPriority` and with `ck_sla_policy_priority_valid` (migration 0033);
# `test_sla_breach.py` compares this tuple against the live CHECK's domain, so
# a value added on one side and not the other fails rather than drifting into
# a priority this ladder cannot place.
PRIORITY_LADDER: tuple[str, ...] = ("low", "normal", "high", "urgent")

Target = Literal["response", "resolution"]

# The three per-target lookups, declared as parallel maps rather than as
# `if target == "response"` branches. Task 5's F1 was exactly a constant
# column-to-clock pairing that every fixture held still: a mutant that stamped
# `response_breached_at` while reading `resolution_due_at` is invisible to any
# test that only ever exercises one target, and is invisible to branchy code
# that repeats the target name three times per branch. Keyed once, the pairing
# is data the tests can enumerate.
_BREACH_COLUMN: dict[str, str] = {
    "response": "response_breached_at",
    "resolution": "resolution_breached_at",
}
_DUE_COLUMN: dict[str, str] = {
    "response": "response_due_at",
    "resolution": "resolution_due_at",
}
# NOT `tracking._response_running` / `_resolution_running`. Those answer "is
# this clock still ticking"; this answers "was this promise broken", and the
# two disagree on exactly the row shape §7's sweep was widened to select --
# `met_at > due_at`, a reply that landed after the deadline. Reading the
# running guard here left every such row selected and skipped on every cycle,
# forever. See `tracking._breachable`'s docstring for the full argument and
# for why the running guard's four callers must keep the narrower question.
_BREACHABLE: dict[str, Callable[[TicketSla], bool]] = {
    "response": _response_breachable,
    "resolution": _resolution_breachable,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _next_priority(current: str) -> str | None:
    """One step up `PRIORITY_LADDER`, or None at the `urgent` ceiling (§6).

    Returns None -- rather than raising or wrapping around -- for `urgent`,
    because "already at the top" is an ordinary outcome the sweep meets on
    every genuinely urgent ticket, not an error. A wrap-around would silently
    *demote* the fleet's most urgent work on breach.

    An off-ladder priority RAISES instead. `ticket.priority` carries no DB
    CHECK (only `sla_policy.priority` does), and `update_ticket` takes a
    `dict[str, Any]`, so a value outside the ladder is reachable by a service
    caller even though `schema.TicketPriority` refuses it at the API. There is
    no defensible answer to "what is one step above `p1`", and the two silent
    alternatives are both worse than a typed error: treating it as the ceiling
    means such a ticket can never escalate and nothing ever says so, and
    guessing an index means escalating a ticket to a priority nobody defined.
    """
    try:
        index = PRIORITY_LADDER.index(current)
    except ValueError:
        raise SlaValidationError(
            tr(
                "ticket priority {current} is not on PRIORITY_LADDER {ladder}; "
                "refusing to guess which step escalation should raise it to",
                current=repr(current),
                ladder=PRIORITY_LADDER,
            )
        ) from None
    if index == len(PRIORITY_LADDER) - 1:
        return None
    return PRIORITY_LADDER[index + 1]


async def mark_breached(
    session: AsyncSession,
    org_id: UUID,
    ticket_sla: TicketSla,
    *,
    target: Target,
) -> None:
    """Stamp `<target>_breached_at`, publish `ticket.sla_breached`, and apply
    the policy's escalation at most once for this ticket (§6).

    `ticket_sla` is the row the sweep already selected; it is re-loaded here
    org-scoped and `FOR UPDATE`, for the same two reasons `tracking.py`'s
    read-modify-writes do it. The lock serialises a concurrent sweep worker
    (or a concurrent `resume`) on the same row instead of letting both read
    the same pre-breach state and one clobber the other; the org predicate is
    invariant #5 (`ticket_sla` is reachable through plain FKs from another
    tenant's ticket). The caller's object is checked against `org_id` in
    memory first, which is strictly stronger than the re-SELECT alone: an
    in-memory comparison cannot be satisfied by a row this org can see under
    a different id.

    A NO-OP WHEN THAT TARGET IS NOT BREACHABLE -- `tracking._breachable`,
    which is `breached_at IS NULL AND (met_at IS NULL OR met_at > due_at)`.
    The two halves fail for different reasons:

    * **Already breached** is §10.7's idempotence: one stamp, one event, one
      escalation, however often an at-least-once sweep sees the row. It holds
      for a late-met row too -- such a row carries BOTH stamps after the first
      mark, and the second mark must still do nothing.
    * **Stopped on time** (`met_at <= due_at`) is the promise being kept: a
      reply a second inside the deadline stops that clock, and stamping a
      breach over it would fire a breach notification for a promise nobody
      broke. `met_at == due_at` counts as kept -- "within 60 minutes" is
      satisfied at minute 60 exactly -- which is also what makes this the
      exact complement of the sweep's strict `met > due`.

    A clock stopped LATE is neither of those and IS stamped. See the module
    docstring: that is the whole point of using the breachable predicate here
    rather than `tracking._response_running`, which every other consumer of
    this row correctly uses and which would skip it.

    The two targets are independent. Marking `resolution` on a row whose
    `response` already breached is ordinary and stamps only the resolution
    column -- but escalates nothing, because `escalated_at` is already set.
    Lateness is independent too: a ticket answered late and resolved early
    breaches `response` and nothing else.

    A NO-OP WHEN THE PROMISE IS VOID -- `tracking.voided_at is not None`, or
    the ticket's own status is in `FINAL_STATUSES` even when `voided_at`
    happens to read NULL (the pre-migration-0038 backlog). Neither stamps nor
    publishes: `note_cancelled`'s own docstring is explicit that a voided
    promise is neither met nor breached, and this is that guarantee enforced
    here rather than only in whichever caller currently remembers to filter
    for it (see the module docstring's "ORG SCOPING" reasoning for why a
    tenancy predicate lives in the function that needs it rather than in its
    caller -- the same argument applies to this invariant).

    Raises `SlaValidationError` for an unknown `target`, a cross-org row, a
    row that has vanished, or a ticket priority that is not on
    `PRIORITY_LADDER`. Raises `TicketNotFoundError` (service-desk) if the
    policy's `escalate_to_queue_id` names a queue in another org -- resolved
    BEFORE either half is applied, precisely so that failure cannot land
    between them.
    """
    if target not in _BREACH_COLUMN:
        raise SlaValidationError(
            tr(
                "unknown SLA target {target}; expected one of {expected}",
                target=repr(target),
                expected=tuple(_BREACH_COLUMN),
            )
        )
    if ticket_sla.org_id != org_id:
        raise SlaValidationError(
            tr(
                "ticket_sla {id} belongs to org {record_org_id}, not {org_id}; refusing to mark "
                "it breached",
                id=ticket_sla.id,
                record_org_id=ticket_sla.org_id,
                org_id=org_id,
            )
        )

    tracking = await _load_tracking(session, org_id, ticket_sla.ticket_id, for_update=True)
    if tracking is None:
        raise SlaValidationError(
            tr(
                "the SLA record for ticket {ticket_id} is not visible in org {org_id}; "
                "refusing to mark it breached",
                ticket_id=ticket_sla.ticket_id,
                org_id=org_id,
            )
        )
    if not _BREACHABLE[target](tracking):
        return

    # A VOIDED PROMISE IS NEITHER MET NOR BREACHED (review sla-breach-wiring,
    # F1). `tracking.note_cancelled` stamps `voided_at` and says exactly that
    # in its own docstring; before this check nothing here enforced it, so a
    # cancelled/rejected ticket handed to this function -- directly, or by a
    # sweep filter that has not (yet, or ever) excluded it -- still got
    # stamped `<target>_breached_at` and still published `ticket.sla_breached`
    # for a promise that had already stopped meaning anything. Checked before
    # `ticket` is even loaded: no ticket state needs inspecting to know a
    # voided row must not be touched.
    if tracking.voided_at is not None:
        return

    ticket = await _load_ticket_in_org(session, org_id, tracking.ticket_id)

    # DEFENCE IN DEPTH FOR THE ROWS `voided_at` CANNOT SEE: migration 0038
    # deliberately did not backfill `voided_at` onto tickets cancelled or
    # rejected before the column existed (it will not fabricate a past
    # instant), so `voided_at IS NULL` alone does not distinguish "never
    # voided" from "voided before this column shipped". `FINAL_STATUSES` is
    # `note_cancelled`'s own voiding domain (`lifecycle.FINAL_STATUSES`,
    # exactly `cancelled`/`rejected`) -- checking the ticket's CURRENT status
    # against it catches that historical backlog without needing the
    # timestamp at all. This is deliberately narrower than `TERMINAL_STATUSES`
    # (`_escalate`'s set, below): `closed` is not in `FINAL_STATUSES` because
    # closing is a discharge, not a void -- a ticket that missed its deadline
    # and was later closed has a real, staff-facing breach in its history, and
    # suppressing the stamp would quietly under-report it (module docstring,
    # "A TERMINAL TICKET IS NEVER ESCALATED"). Only a promise the ticket core
    # itself declares void is skipped here.
    if ticket.status in FINAL_STATUSES:
        return

    now = _utcnow()
    setattr(tracking, _BREACH_COLUMN[target], now)
    await session.flush()

    # Published BEFORE escalation, with the pre-escalation queue/priority --
    # see the module docstring; `routing.resolve_recipients` routes on
    # `payload["queue_id"]`, so this decides WHO is told, not just what they
    # read. No `requester_id` key, ever (§6.1).
    due_at: datetime = getattr(tracking, _DUE_COLUMN[target])
    await publish(
        session,
        org_id=org_id,
        event_type="ticket.sla_breached",
        subject_type="ticket",
        subject_id=ticket.id,
        payload={
            "ticket_id": str(ticket.id),
            "ticket_number": ticket.number,
            "subject": ticket.subject,
            "target": target,
            "due_at": due_at.isoformat(),
            "policy_id": str(tracking.policy_id),
            "queue_id": str(ticket.queue_id),
            "priority": ticket.priority,
        },
        actor_user_id=None,
        client_id=ticket.client_id,
    )

    await _escalate(session, org_id, tracking, ticket, target=target, now=now)


async def _escalate(
    session: AsyncSession,
    org_id: UUID,
    tracking: TicketSla,
    ticket: Ticket,
    *,
    target: Target,
    now: datetime,
) -> None:
    """Apply the row's policy's escalation, once, without recomputing
    anything (§6, §6.0, §6.0.1).

    Every early return below is a case where nothing is applied AND
    `escalated_at` stays NULL -- the latch records that an escalation
    happened, not that one was considered:

    * already escalated (§10.7's latch),
    * the policy enables neither half,
    * the ticket is terminal (module docstring: the union of the two
      services' refusals, so no half-applied escalation is possible),
    * the priority is at the `urgent` ceiling and there is no queue move,
    * the escalation queue is the queue the ticket is already in -- moving a
      ticket to where it already is would write a `ticket.assigned` audit row
      and publish a `ticket.assigned` event for a change that did not happen.

    THE POLICY IS LOADED BY ID, NEVER RE-RESOLVED. `resolve_policy` keys on
    `(queue, priority)`, so calling it here -- before the bump, after it, or
    after the queue move -- is §0.4's cascade. This module imports the row's
    own policy through `tracking._load_policy_in_org` (which deliberately does
    not filter `active`: a retired policy still governs the tickets already
    under it) and reads only its two escalation switches from it.

    THE ESCALATION QUEUE IS RESOLVED BEFORE EITHER HALF IS APPLIED.
    `sla_policy.escalate_to_queue_id` is a plain FK with no org check, so it
    can legally name another tenant's queue (invariant #5). `assign_ticket`
    would refuse it -- but only at the point of the call, which on a combined
    escalation is *after* the priority bump has already been applied to the
    ticket. Resolving first collapses the last remaining way the pair could
    half-apply into a failure that happens before anything is written.
    """
    if tracking.escalated_at is not None:
        return

    policy = await _load_policy_in_org(session, org_id, tracking.policy_id)
    if not policy.escalate_priority and policy.escalate_to_queue_id is None:
        return

    if ticket.status in TERMINAL_STATUSES:
        return

    old_priority = ticket.priority
    old_queue_id = ticket.queue_id

    new_priority = _next_priority(old_priority) if policy.escalate_priority else None
    new_queue_id: UUID | None = None
    if policy.escalate_to_queue_id is not None and policy.escalate_to_queue_id != old_queue_id:
        new_queue_id = (await resolve_queue(session, org_id, policy.escalate_to_queue_id)).id

    if new_priority is None and new_queue_id is None:
        # A self-queue no-op (D3/D4, review sla-breach-wiring F6): the
        # policy's escalation queue IS the ticket's current queue and
        # priority escalation is off (or already at the ceiling), so there is
        # genuinely nothing to apply. `escalated_at` deliberately stays NULL
        # here rather than being stamped for a no-op -- the docstring's early
        # returns above are all cases where nothing happened AND the latch is
        # not set. The consequence is that this target's breach is
        # reconsidered on the ticket's NEXT breach (e.g. the resolution clock
        # breaching after the response clock already did): judged deliberate
        # and benign, not a latch bug -- do not "fix" it by stamping
        # `escalated_at` here, which would make a later, genuinely-actionable
        # escalation (the queue having changed since, say) look already done.
        return

    # §6.0's table, exactly: priority through `update_ticket` (audit action
    # `ticket.updated`), queue through `assign_ticket` (audit action
    # `ticket.assigned`), both `actor_id=None` positional with keyword-only
    # `actor_type="system"`. Neither call recomputes a deadline, because
    # neither this module nor those services touch `ticket_sla`.
    if new_priority is not None:
        await update_ticket(
            session, org_id, None, ticket.id, {"priority": new_priority}, actor_type="system"
        )
    if new_queue_id is not None:
        await assign_ticket(
            session, org_id, None, ticket.id, {"queue_id": new_queue_id}, actor_type="system"
        )

    tracking.escalated_at = now
    # §0.4's attribution: the deadlines on this row were NOT recomputed, and
    # this is the column that says so. `policy_id`/`calendar_id` stay pointed
    # at the policy that produced them -- see the module docstring.
    tracking.policy_source = "escalation"
    await session.flush()

    # §6.0.1 / §8: escalation is one of the explicitly audited privileged
    # actions (invariant #4), and the two service-level rows are not enough on
    # their own -- `ticket.updated` records only a changed-field NAME and
    # `ticket.assigned` only the new queue, so neither says which SLA target
    # breached nor what the value was before. `old_*`/`new_*` are the real
    # before/after values and are equal when that half did not apply, so a
    # reader diffs them rather than trusting a separate boolean to agree.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="system",
        actor_id=None,
        action="sla.escalated",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=ticket.client_id,
        metadata={
            "ticket_number": ticket.number,
            "target": target,
            "policy_id": str(policy.id),
            "old_priority": old_priority,
            "new_priority": new_priority if new_priority is not None else old_priority,
            "old_queue_id": str(old_queue_id),
            "new_queue_id": str(new_queue_id) if new_queue_id is not None else str(old_queue_id),
        },
    )
    await session.flush()
