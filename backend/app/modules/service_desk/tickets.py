"""Ticket core: create, get, list, update, and move to another client
(docs/modules/service-desk/spec.md §2, §3, §5, §7.2, §8; MSP clients spec
§2.4 for the move).

Mirrors `app.modules.budget.expenses`'s style closely -- it solved every
problem here already: org-scoped load + audit + flush for every mutation
(invariant #4), an explicit `org_id` predicate on every query (invariant #5),
and a typed `TicketValidationError`/`TicketNotFoundError` pre-check in place
of letting a DB constraint or an unresolved cross-reference surface as a 500.

**Status is never touched here.** `initial_status` decides the status a new
ticket starts in (via `lifecycle.py`, the sole authority per spec §3.3); every
status change AFTER creation goes through `transitions.py`'s `change_status`,
which is what runs `lifecycle.assert_transition` and writes
`ticket_status_history`. `update_ticket`'s editable-field set therefore
excludes `status` entirely -- and a caller that includes it in `fields` is
REFUSED with a typed `TicketValidationError` naming `change_status`, not
silently ignored (see `_reject_non_editable_fields`).

**`assigned_to`/`queue_id` are likewise never touched here (task 8).** Spec §5
requires both to need `ticket.assign`, distinct from this module's
`ticket.write`-gated PATCH, and spec §8.3 files the change under
`ticket.assigned`, not `ticket.updated`. Both are absent from
`TICKET_EDITABLE_FIELDS`; `transitions.assign_ticket` is their only write
path AFTER CREATION -- `create_ticket` below still writes `assigned_to`
directly from `TicketCreate.assigned_to` (task 8's S6: no live exploit today
since no role holds `write` without `assign`, but a future route author must
not treat "assign_ticket is the only write path" as an invariant that already
holds). A caller that includes either key in `update_ticket`'s `fields` is
refused and pointed at `assign_ticket`, the same treatment `status` gets.

**That refusal is the SLA task 8 review's F1 fix.** These three keys used to
be discarded in silence, which meant SLA §6's queue-move escalation would
have called `update_ticket({"queue_id": ...})`, got a 200 and an unchanged
ticket, and reported success. `queue_id` is *not* being re-added to the
editable set to accommodate that -- the gating and audit-action reasons it
was removed (commit `eb6b129`) are unchanged, and queue is load-bearing for
SLA policy resolution, which keys on `(queue, priority)`. Escalation calls
`transitions.assign_ticket` instead, which is what the error message says.

**Ordering (the load-bearing rule for this file, per `expenses.py`'s
docstring).** `AsyncSession` defaults to `autoflush=True`, and a bare
`select()` (any `resolve_*` call, `numbering.next_number`'s lookups) flushes
every pending object on the session. `create_ticket` therefore resolves every
cross-reference (category, queue, requester, assignee, device) AND allocates
the ticket number BEFORE constructing or adding the `Ticket` ORM object --
never `session.add(ticket)` first. The same ordering applies inside
`update_ticket`: every cross-reference is resolved in a pass that performs no
`setattr` on the already-tracked `Ticket`, so a cross-org reference never has
to unwind a mutation that already happened.

**The Phase 15 contract (spec §7.2, context note #6).** `origin="alert"` is
the one case where `requester_id` and `created_by` are BOTH `None` -- the DB
check constraint `ck_ticket_human_attribution` requires both non-null for
every other origin. `create_ticket` enforces this itself rather than trusting
the caller: for `origin="alert"` it forces `requester_id=None`,
`created_by=None`, and writes the creation's audit event and status-history
row with `actor_type="system"`, `actor_id=None`, regardless of what
`data.requester_id`/`actor_id` were passed. For every other origin, a missing
`requester_id` or a missing `actor_id` is rejected as a typed
`TicketValidationError` before anything is resolved or written -- letting
either through as `None` would only surface as the check constraint's
`IntegrityError` (a 500) at flush time.

Two more corners of that same contract are pinned up front, before anything
is resolved: spec §7.2 declares alert-opened tickets are `type="incident"`,
so `origin="alert"` paired with any other `data.type` is a typed
`TicketValidationError`, not a silently-accepted request-type ticket with no
human on it; and spec §2 declares `source_alert_id` NULL for every ticket
until Phase 15 actually dispatches alerts, so a non-`None` `source_alert_id`
on any origin other than `"alert"` is rejected the same way, rather than
persisting an origin/reference-column combination the spec doesn't
recognize.

**`origin="integration"` maps to `actor_type="system"` for the CREATE
action's attribution, exactly like `"alert"` -- but is NOT one of the
origins exempted from human attribution (whole-branch security review,
finding A3).** `service_desk.adapters.ServiceDeskChangeTicketAdapter
.push_change` auto-opens a ticket when a change transitions, attributed to
the change's own author -- but the ACT of creating that ticket is the system
reacting to someone else's later transition, not that author sitting at a
keyboard filing a helpdesk ticket. Origin `"technician"` (what `push_change`
used before this fix) made `ticket_status_history`/the `ticket.created`
audit event read `actor_type="user"`, `actor_id=<the author>` -- in the
immutable hash-chained log, indistinguishable from that person filing the
ticket by hand, however much later the actual triggering transition
happened. `origin="integration"` fixes exactly that attribution while
changing nothing else: `requester_id`/`created_by` (the `Ticket` columns)
still require and carry a real human id, same as `"technician"`/`"portal"`/
`"email"` -- only the CREATE-action's `actor_type`/history-and-audit
`actor_id` become `"system"`/`None`, the same pairing `close_change` already
uses for its own system-driven transitions.

**Audit metadata never carries subject or description bodies (spec §8.3,
context note #8).** A user can type anything into those two fields; the audit
log is not the place to duplicate them. `ticket.created`'s metadata is built
only from identifiers (`type`, `category_id`, `queue_id`, `origin`, `number`);
`ticket.updated`'s metadata is `{"changed_fields": [...]}` -- field NAMES
only, the identical shape `expense.updated` uses.

**PATCH semantics (the Phase-12 lesson, carried from `expenses.py`).**
`update_ticket` receives a plain `dict` (the route layer will pass
`TicketUpdate.model_dump(exclude_unset=True)`) -- a key's PRESENCE, not its
value, decides whether a field is touched. An explicit `None` on a
NON-NULLABLE column (`NON_NULLABLE_TICKET_FIELDS`) is rejected in a pass that
runs BEFORE any `setattr`, so the ORM object is never left partially mutated
when that rejection raises.
"""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.events import publish
from app.core.i18n import tr
from app.core.rbac import has_permission
from app.core.ticket_words import status_quoted, type_quoted
from app.models import (
    Device,
    HealthAlert,
    HealthAlertAction,
    Ticket,
    TicketApproval,
    TicketStatusHistory,
    User,
)
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.service_desk.errors import (
    TicketClientError,
    TicketConflictError,
    TicketNotFoundError,
    TicketValidationError,
)
from app.modules.service_desk.lifecycle import TERMINAL_STATUSES, initial_status
from app.modules.service_desk.numbering import next_number
from app.modules.service_desk.refdata import resolve_category, resolve_queue
from app.modules.service_desk.schema import TicketCreate, TicketOrigin
from app.modules.sla.due import next_due_clause
from app.modules.sla.models import TicketSla
from app.modules.sla.tracking import (
    recompute_for_retriage,
    sla_misconfiguration_is_not_fatal,
    start_tracking,
)
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

# update_ticket's editable-field set, decided by key PRESENCE in `fields` (the
# `budget.expenses`/`refdata.py` convention -- callers pass
# `TicketUpdate.model_dump(exclude_unset=True)`). `status` is deliberately
# absent: status only ever moves through `transitions.change_status`, its own
# audited action, never a side effect of a generic PATCH (spec §9, and this
# module's docstring above). `type`, `requester_id`, `origin`, and
# `source_alert_id` are likewise absent -- immutable after creation.
#
# `assigned_to` and `queue_id` are ALSO deliberately absent (moved out here,
# task 8's context note #3): spec §5 requires both to need `ticket.assign`,
# and §8.3 files the change under `ticket.assigned`, not `ticket.updated`.
# Leaving them editable through this `ticket.write`-gated PATCH path would
# make the `ticket.assign` permission decorative and misfile assignment
# history under the wrong audit action. `transitions.assign_ticket` is their
# only write path AFTER CREATION -- `create_ticket` above still writes
# `assigned_to` itself, a deliberate (if narrow) carve-out, not an oversight
# to paper over here; a caller that includes either key in `fields` for THIS
# function is REFUSED and pointed at `assign_ticket`, identical to how
# `status` is refused and pointed at `change_status`
# (`_reject_non_editable_fields`, SLA task 8 review F1).
# The list's two orders: newest first, or earliest outstanding SLA deadline.
TicketSort = Literal["newest", "due"]

TICKET_EDITABLE_FIELDS: tuple[str, ...] = (
    "category_id",
    "priority",
    "subject",
    "description",
    "device_id",
)

# NOT NULL columns on `ticket` that are also in `TICKET_EDITABLE_FIELDS`.
# `device_id` is genuinely nullable and is deliberately absent: an explicit
# `null` there is a caller's request to CLEAR the field, and must actually
# clear it (mirrors `expenses.py`'s `NON_NULLABLE_EXPENSE_FIELDS` reasoning
# exactly). `queue_id` is NOT NULL but is no longer in
# `TICKET_EDITABLE_FIELDS` at all (see above), so it is not listed here either.
NON_NULLABLE_TICKET_FIELDS: frozenset[str] = frozenset(
    {"category_id", "priority", "subject", "description"}
)

# Fields that live on `ticket` and ARE writable after creation, just not
# through this generic PATCH -- each has a dedicated, differently-gated,
# differently-audited service. `_reject_non_editable_fields` names the right
# service in the error so a caller is never left guessing which door to use
# (SLA task 8 review, F1). Every other rejected key is either immutable after
# creation (`type`, `requester_id`, `origin`, `source_alert_id`) or a typo.
_TICKET_FIELD_REDIRECTS: dict[str, str] = {
    "queue_id": "transitions.assign_ticket",
    "assigned_to": "transitions.assign_ticket",
    "status": "transitions.change_status",
}


def _reject_non_editable_fields(fields: dict[str, Any]) -> None:
    """A key that is not in `TICKET_EDITABLE_FIELDS` is REFUSED, not ignored.

    This function is the fix for the SLA task 8 review's F1. `queue_id`,
    `assigned_to`, and `status` were all *silently discarded* here: the call
    returned normally, the ticket had not moved, `changed_fields` said
    nothing, and the caller believed it had succeeded. Commit `eb6b129`
    already made exactly this argument one layer up when it deleted
    `queue_id`/`assigned_to` from `schema.TicketUpdate` -- "a silent no-op is
    worse than an error, because it is an error the client cannot see" -- but
    only the HTTP layer was closed. The service stayed silent, and SLA §6's
    queue-move escalation was about to call it directly, one layer below the
    schema that would have caught it, and self-report success.

    The whitelist still decides what may be WRITTEN, so this widens nothing
    and reaches no new column; it only converts "discarded, no-op audit
    event, success" into a typed `TicketValidationError`. Same treatment, and
    very nearly the same message, as `sla.policies.update_policy`'s own
    unknown-key refusal (SLA task 4 review, F6).

    It runs FIRST -- before the null check, before the row is loaded, before
    any `setattr` -- so a refused PATCH mutates nothing at all, including the
    editable half of a mixed request. Applying the editable half and dropping
    the rest would be the same silent partial success in a smaller costume.
    """
    refused = sorted(set(fields) - set(TICKET_EDITABLE_FIELDS))
    if not refused:
        return
    detail = ", ".join(
        tr("{field} (use {redirect})", field=field, redirect=_TICKET_FIELD_REDIRECTS[field])
        if field in _TICKET_FIELD_REDIRECTS
        else field
        for field in refused
    )
    editable = ", ".join(TICKET_EDITABLE_FIELDS)
    if len(refused) == 1:
        raise TicketValidationError(
            tr(
                "field not editable via update_ticket: {detail}; editable fields are {editable}",
                detail=detail,
                editable=editable,
            )
        )
    raise TicketValidationError(
        tr(
            "fields not editable via update_ticket: {detail}; editable fields are {editable}",
            detail=detail,
            editable=editable,
        )
    )


def _reject_null_for_non_nullable(fields: dict[str, Any], non_nullable: frozenset[str]) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `budget.expenses`/`refdata.py` pattern). Runs
    BEFORE any `setattr` so the ORM object is never left partially mutated
    when this raises."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise TicketValidationError(tr("{field} cannot be null", field=field))


def _reject_if_terminal(ticket: Ticket) -> None:
    """Spec §3.3: `TERMINAL_STATUSES` (`closed`, `cancelled`, `rejected`)
    gates FIELD EDITS ONLY, never the status-transition path -- `closed` is
    deliberately reopenable via `transitions.change_status`, which does not
    call this function. Gating edits on `lifecycle.FINAL_STATUSES` instead
    would wrongly let a closed ticket's fields keep changing; gating the
    transition path on this set instead would wrongly make every reopen
    impossible (context note #2). This function is `update_ticket`'s guard
    only."""
    if ticket.status in TERMINAL_STATUSES:
        raise TicketValidationError(
            tr(
                "ticket {id} is {status} and cannot be edited",
                id=ticket.id,
                status=status_quoted(ticket.status),
            )
        )


async def resolve_user(session: AsyncSession, org_id: UUID, user_id: UUID) -> User:
    """Org-scoped load. A user id from another org is indistinguishable from
    a nonexistent one -- both raise `TicketNotFoundError` (spec §5, §8,
    invariant #5). Used for `requester_id` and `assigned_to` here, and
    imported by `transitions.assign_ticket` for its own assignee resolution
    (task 8's S8: was a verbatim copy there, `_resolve_assignee`; promoted
    and shared rather than kept as two copies of the same org-scoping
    predicate)."""
    user = (
        await session.execute(select(User).where(User.org_id == org_id, User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise TicketNotFoundError(tr("user {user_id} not found", user_id=user_id))
    return user


async def _resolve_device(session: AsyncSession, org_id: UUID, device_id: UUID) -> Device:
    """Org-scoped load. A device id from another org is indistinguishable
    from a nonexistent one -- both raise `TicketNotFoundError` (spec §7.3,
    §8, invariant #5)."""
    device = (
        await session.execute(
            select(Device).where(Device.org_id == org_id, Device.id == device_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise TicketNotFoundError(tr("device {device_id} not found", device_id=device_id))
    return device


# ---------------------------------------------------------------------------
# The ticket's client (MSP clients spec §2.4, §3.2; plan 2026-10-26 Task 2)
# ---------------------------------------------------------------------------


async def _new_ticket_client(
    session: AsyncSession,
    org_id: UUID,
    *,
    device: Device | None,
    claimed: UUID | None,
    origin: TicketOrigin,
) -> UUID:
    """The client a NEW ticket is filed under -- the rule Tasks 3 and 4 reuse.

    With a device, the device's: a device's client never changes
    (`its_device_client_guard`), and `its_ticket_client_guard` holds the
    ticket to it. Without one, the one the caller names. Neither, or both and
    different, is a 422: a contradicting client is refused, never silently
    corrected, because the caller believed something false about the device.

    The client is then loaded as every other new row's is
    (`client_for_new_rows`, held FOR SHARE until commit): one the session
    cannot see -- another organisation's, or outside a narrowed technician's
    scope -- is a 404, never a 403 (spec §3.2's existence oracle), and an
    archived one a 409. "Sin asignar" takes a ticket only from email (C10:
    it is the inbox of what has no owner, not a client anyone files under).

    The portal skips the load: `portal_user` cannot read `clients` (0028).
    `portal.tickets.create_my_ticket` derives the client from the requester
    itself and checks it is inside the portal session's scope.
    """
    if device is not None:
        if claimed is not None and claimed != device.client_id:
            raise TicketClientError(tr("the ticket's client is not its device's client"))
        ticket_client = device.client_id
    elif claimed is None:
        raise TicketClientError(tr("choose the client this ticket belongs to"))
    else:
        ticket_client = claimed
    if origin != "portal":
        try:
            client = await client_for_new_rows(session, org_id=org_id, client_id=ticket_client)
        except ClientNotFound:
            raise TicketNotFoundError(tr("no such client")) from None
        except NotAcceptingNewRows as exc:
            raise TicketConflictError(exc.detail) from None
        if client.kind == CLIENT_KIND_UNASSIGNED and origin != "email":
            raise TicketClientError(tr("tickets reach Unassigned only by email"))
    return ticket_client


def _requester_belongs(requester: User, client_id: UUID) -> None:
    """A CONTACT requester is one of the ticket's client's own people
    (research-1 §E2). Otherwise that contact's emailed reply -- matched on
    `ticket.requester_id` and written under scope all -- would comment on
    another client's ticket. A staff requester belongs to no client and may
    file for any. `its_ticket_client_guard` says the same at the database."""
    if requester.kind == "contact" and requester.client_id != client_id:
        raise TicketClientError(tr("the requester is a contact of another client"))


async def ensure_assignee_sees(session: AsyncSession, assignee: User, client_id: UUID) -> None:
    """An assignee is a technician who holds `ticket.read` in the ticket's
    client (research-1 §E3): a ticket handed to somebody who cannot open it
    is work nobody can do, and a contact is never a technician. Shared by
    `create_ticket` and `transitions.assign_ticket`.

    `has_permission` with the record's client (plan Global Constraints):
    an all-clients grant counts, a grant on this client counts, one on
    another client does not. It reads the ASSIGNEE's grants, not the
    caller's, so it is a fact about a record, never a request scope."""
    if assignee.kind != "staff" or not await has_permission(
        session, assignee.id, "ticket.read", client_id=client_id
    ):
        raise TicketClientError(tr("the assignee cannot see this client's tickets"))


async def hold_ticket_client(
    session: AsyncSession, ticket_id: UUID, *, moving: bool = False
) -> None:
    """Hold the ticket's client still until commit (research-1 §I5).

    A row inserted under a ticket -- a comment, an attachment -- takes the
    ticket's client from `its_client_from_parent`, which reads the ticket
    with NO lock; and a move's `its_client_to_children` updates only the
    children it can see. A comment written while a move is in flight
    therefore reads the old client, and the move cannot see it to carry it
    over: it would stay in the old client for good. `add_comment` and
    `add_attachment` call this (shared) before they read the ticket, and
    `move_ticket` (`moving=True`, exclusive) before it reads it to change it:
    a child waits for a move in flight and is then born in the new client; a
    move waits for the children in flight and then carries them over.

    A transaction-scoped ADVISORY lock, not `SELECT ... FOR SHARE` on the
    ticket: a row lock needs the UPDATE privilege, and `portal_user` -- whose
    replies and uploads reach `add_comment`/`add_attachment` -- holds only
    SELECT and INSERT on `ticket` (0028), deliberately. The same
    `hashtext` key shape as the IPAM, remote-screen and push-run locks."""
    lock = "pg_advisory_xact_lock" if moving else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {lock}(hashtext('ticket-client:' || :ticket))"),
        {"ticket": str(ticket_id)},
    )


# Origins whose CREATE-action attribution is `actor_type="system"` (module
# docstring, finding A3): the ticket wasn't created by a human sitting at a
# keyboard, so the `ticket_status_history` row and the `ticket.created` audit
# event must not read `actor_type="user"`. `"alert"` additionally has no
# human requester/author at all (`ck_ticket_human_attribution`'s exemption,
# handled separately below); `"integration"` still requires and keeps a real
# `requester_id`/`created_by` -- only this attribution changes.
_SYSTEM_ATTRIBUTED_ORIGINS: frozenset[str] = frozenset({"alert", "integration"})


# ---------------------------------------------------------------------------
# create_ticket
# ---------------------------------------------------------------------------


async def create_ticket(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    data: TicketCreate,
    *,
    origin: TicketOrigin = "technician",
    source_alert_id: UUID | None = None,
    client_id: UUID | None = None,
) -> Ticket:
    """Create a ticket (spec §2, §3, §5, §7.2).

    `client_id` is the client a path that is not a technician's decides on
    (the portal, email, an alert, a change: MSP clients plan 2026-10-26
    Task 2); when it is None, the request's `data.client_id` is the claim. A
    device decides the client either way (`_new_ticket_client`).

    `origin`/`source_alert_id` are explicit keyword arguments, never read off
    `data` -- `TicketCreate` has no `origin` field at all (see `schema.py`'s
    note on why it was removed), so a technician-facing route cannot let a
    request body smuggle `origin="alert"` (and its relaxed
    human-attribution rule) past whatever the route actually intends to
    create; the CALLER decides the origin, not the request body. Typing
    `origin` as `TicketOrigin` (the same `Literal` `schema.py` bounds the
    request-facing fields with) means mypy rejects an unrecognized string at
    every call site, rather than letting a typo like `"alerts"` reach
    Postgres unrejected.

    Resolution order, all BEFORE the `Ticket` object is constructed (ordering
    rule above): category -> queue (category default, overridden by an
    explicit `data.queue_id`) -> requester (only for non-alert origin) ->
    assignee (if supplied) -> device (if supplied) -> the client (the
    device's, else the one named; the requester and the assignee are then
    checked against it) -> the ticket number.
    `initial_status` is computed from `data.type` and the resolved category's
    `requires_approval` last, right before the object is built.

    **Spec §4: a request landing on `pending_approval` gets its
    `ticket_approval` row created HERE, in the same transaction as the
    ticket itself (task 9's decision -- `decide_approval` in `approvals.py`
    only ever reads and stamps this row, it never creates one).** The spec
    is explicit that creation is what makes the row: "On creating a request
    whose category has `requires_approval = True`, the service creates a
    `ticket_approval` row in `pending`". Creating it anywhere else -- e.g.
    lazily inside `decide_approval` -- would duplicate the
    "does this ticket need an approval row" decision across two files
    (exactly the failure mode this module's docstring already warns about
    for the status machine), and would leave a real gap for a ticket that is
    read or listed while `pending_approval` but before anyone has ever tried
    to decide it: no row would exist yet for a UI or report to show. Doing it
    here means the row and the ticket's entry into `pending_approval` are one
    atomic fact from the moment either is observable. At most one row per
    ticket is `uq_ticket_approval_org_ticket`'s job at the database level;
    `create_ticket` running exactly once per ticket is what guarantees it is
    only ever asked to insert once.
    """
    # Spec §7.2/§2 (Phase 15 contract, see module docstring): both checks run
    # before anything is resolved, since neither depends on a DB lookup.
    if origin == "alert" and data.type != "incident":
        raise TicketValidationError(
            tr("origin='alert' tickets must be type='incident', got {type}", type=repr(data.type))
        )
    if source_alert_id is not None and origin != "alert":
        raise TicketValidationError(
            tr(
                "source_alert_id is only valid for origin='alert', got origin={origin}",
                origin=repr(origin),
            )
        )

    category = await resolve_category(session, org_id, data.category_id)

    if data.type != category.type:
        raise TicketValidationError(
            tr(
                "ticket type {type} does not match category type {category_type}",
                type=type_quoted(data.type),
                category_type=type_quoted(category.type),
            )
        )

    # Queue: an explicit queue_id overrides the category's default; if
    # neither resolves, this is a typed validation error rather than landing
    # the ticket somewhere arbitrary (spec §5, context note #5).
    if data.queue_id is not None:
        queue = await resolve_queue(session, org_id, data.queue_id)
    elif category.default_queue_id is not None:
        queue = await resolve_queue(session, org_id, category.default_queue_id)
    else:
        raise TicketValidationError(
            tr(
                "category {id} has no default_queue_id and no queue_id was supplied",
                id=category.id,
            )
        )

    # Human attribution (spec §2, §7.2, context note #6): origin="alert" is
    # the ONE case with no human requester or author -- forced here
    # regardless of what data.requester_id/actor_id carry, so the contract
    # holds even if a future caller passes them by mistake. Every other
    # origin requires both, checked here so the failure is a typed 400
    # rather than `ck_ticket_human_attribution`'s IntegrityError (a 500).
    requester: User | None = None
    if origin == "alert":
        requester_id: UUID | None = None
        created_by: UUID | None = None
    else:
        if data.requester_id is None:
            raise TicketValidationError(
                tr(
                    "requester_id is required for origin={origin}",
                    origin=repr(origin),
                )
            )
        if actor_id is None:
            raise TicketValidationError(
                tr(
                    "actor_id is required for origin={origin}",
                    origin=repr(origin),
                )
            )
        requester = await resolve_user(session, org_id, data.requester_id)
        requester_id = requester.id
        created_by = actor_id

    assignee: User | None = None
    if data.assigned_to is not None:
        assignee = await resolve_user(session, org_id, data.assigned_to)

    device: Device | None = None
    if data.device_id is not None:
        device = await _resolve_device(session, org_id, data.device_id)

    ticket_client = await _new_ticket_client(
        session,
        org_id,
        device=device,
        claimed=client_id if client_id is not None else data.client_id,
        origin=origin,
    )
    if requester is not None:
        _requester_belongs(requester, ticket_client)
    if assignee is not None:
        await ensure_assignee_sees(session, assignee, ticket_client)

    status = initial_status(data.type, requires_approval=category.requires_approval)

    # Race-safe allocation (spec §2.1) -- atomic by construction, never a
    # bare SELECT of the counter. Computed AFTER `initial_status` so a raise
    # from an unrecognized type never burns a ticket number -- unreachable
    # today (`data.type` is a closed `Literal`, and `resolve_category`'s
    # match check above already agrees with it), but the ordering costs
    # nothing and removes the trap for whenever that stops being true.
    number = await next_number(session, org_id, data.type)

    actor_type: Literal["user", "system"] = (
        "system" if origin in _SYSTEM_ATTRIBUTED_ORIGINS else "user"
    )
    history_actor_id = None if origin in _SYSTEM_ATTRIBUTED_ORIGINS else actor_id

    ticket = Ticket(
        org_id=org_id,
        client_id=ticket_client,
        number=number,
        type=data.type,
        category_id=category.id,
        queue_id=queue.id,
        status=status,
        priority=data.priority,
        subject=data.subject,
        description=data.description,
        requester_id=requester_id,
        assigned_to=assignee.id if assignee is not None else None,
        device_id=device.id if device is not None else None,
        origin=origin,
        source_alert_id=source_alert_id,
        created_by=created_by,
    )
    session.add(ticket)
    await session.flush()

    # Every transition, including creation, gets a status-history row
    # (spec §2 `ticket_status_history`, §3.3) -- `from_status=None` marks it
    # as the first one.
    session.add(
        TicketStatusHistory(
            org_id=org_id,
            ticket_id=ticket.id,
            from_status=None,
            to_status=status,
            actor_type=actor_type,
            actor_id=history_actor_id,
            reason="",
        )
    )

    # Spec §4 (task 9's decision, see docstring above): a request whose
    # category requires approval always lands in `pending_approval` here --
    # `initial_status` returns it only in that case -- so this is exactly
    # the set of tickets that need a fresh `pending` `ticket_approval` row.
    if status == "pending_approval":
        session.add(
            TicketApproval(
                org_id=org_id,
                ticket_id=ticket.id,
                state="pending",
            )
        )
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=history_actor_id,
        action="ticket.created",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=ticket.client_id,
        # Identifiers only -- never subject/description (spec §8.3, context
        # note #8).
        metadata={
            "type": ticket.type,
            "category_id": str(category.id),
            "queue_id": str(queue.id),
            "origin": origin,
            "number": ticket.number,
        },
    )
    await session.flush()

    # Notification outbox (docs/modules/notification/spec.md §3, §6.4). A
    # request landing on `pending_approval` right here is the moment someone
    # must be told to go decide it -- `ticket.approval_requested` fires on
    # ENTRY to that state, not on the decision (that is
    # `approvals.decide_approval`'s own, distinct `ticket.approval_decided`).
    # Same transaction as the mutation above -- `publish()` neither commits
    # nor opens its own transaction. Payload shape matches
    # `notification.routing.resolve_recipients`'s fixed contract: every id a
    # `str(uuid)` or `None`, `requester_id` present unconditionally.
    if status == "pending_approval":
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
            client_id=ticket.client_id,
        )
    await session.flush()

    # SLA materialisation (docs/modules/sla/spec.md §3, and §1's boundary
    # note: "both create tickets through the ticket core; an SLA record
    # follows from that automatically because §3's resolution runs at ticket
    # creation regardless of origin"). THIS IS THE ONLY CREATION-TIME CALLER,
    # deliberately: putting it here rather than in each of the five surfaces
    # that create tickets (the technician route, the portal, email intake,
    # the change-management adapter, and Phase 15's alert path) is what stops
    # a ticket's SLA lifecycle from depending on which door it came in
    # through. There is no `origin` branch here and there must never be one.
    #
    # Runs LAST, after the ticket, its history row, its approval row, the
    # audit event and the outbox publish -- so the SLA is materialised against
    # a ticket that is already fully written, and `ticket.created_at` (which
    # every deadline is measured from, §0.3) is the flushed database value
    # rather than a half-populated object.
    #
    # A ticket whose queue/priority resolves to NO policy gets NO row and that
    # is correct, not a failure (§3): an org that has configured no policy
    # must visibly have no SLA. `start_tracking` returns `None` for it and
    # nothing here treats that as an error.
    #
    # Wrapped because an SLA failure may not fail a ticket create -- see
    # `sla_misconfiguration_is_not_fatal`'s docstring for the trade.
    async with sla_misconfiguration_is_not_fatal("start_tracking", ticket.id):
        await start_tracking(session, org_id, ticket)
    return ticket


# ---------------------------------------------------------------------------
# get_ticket / list_tickets
# ---------------------------------------------------------------------------


async def get_ticket(
    session: AsyncSession, org_id: UUID, ticket_id: UUID, *, fresh: bool = False
) -> Ticket:
    """Org-scoped load. A ticket id from another org is indistinguishable
    from a nonexistent one -- both raise `TicketNotFoundError` (spec §8,
    invariant #5).

    `fresh` re-reads the row's columns into a ticket this session already
    holds, as `hold_ticket_client`'s callers need: the client they file under
    is the one a move that just committed wrote, not the one loaded before."""
    stmt = select(Ticket).where(Ticket.org_id == org_id, Ticket.id == ticket_id)
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    ticket = (await session.execute(stmt)).scalar_one_or_none()
    if ticket is None:
        raise TicketNotFoundError(tr("ticket {ticket_id} not found", ticket_id=ticket_id))
    return ticket


async def list_status_history(
    session: AsyncSession, org_id: UUID, ticket_id: UUID
) -> list[TicketStatusHistory]:
    """List a ticket's status-transition history, oldest first (spec §2
    `ticket_status_history`, §3.3, §11 -- "status history" in the ticket
    detail view). Org-scoped load of the ticket first, so a cross-org
    `ticket_id` raises `TicketNotFoundError` rather than silently returning
    an empty list -- mirrors `threads.list_comments` exactly, including the
    ordering: oldest first, tie-broken on `id`, matching every history
    write's insertion order (`create_ticket`'s `from_status=None` row, then
    one row per `transitions.change_status` call -- neither ever backdates
    `created_at`)."""
    await get_ticket(session, org_id, ticket_id)

    stmt = (
        select(TicketStatusHistory)
        .where(TicketStatusHistory.org_id == org_id, TicketStatusHistory.ticket_id == ticket_id)
        .order_by(TicketStatusHistory.created_at, TicketStatusHistory.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_tickets(
    session: AsyncSession,
    org_id: UUID,
    *,
    type: str | None = None,
    status: str | None = None,
    queue_id: UUID | None = None,
    assigned_to: UUID | None = None,
    requester_id: UUID | None = None,
    device_id: UUID | None = None,
    client_id: UUID | None = None,
    priority: str | None = None,
    q: str | None = None,
    unassigned: bool = False,
    open_only: bool = False,
    sort: TicketSort = "newest",
    limit: int = 50,
    offset: int = 0,
) -> list[Ticket]:
    """Filterable ticket list (spec §5, §9). Explicit ORDER BY -- newest
    first, tie-broken on `id` -- so a caller asserting list equality depends
    on that, never on incidental insertion/scan order (mirrors
    `budget.expenses.list_expenses`).

    `q` (whole-branch review round C's C1 fix) is a case-insensitive
    substring match over `subject` OR `description` -- an ILIKE on either
    column, not an exact match and not a full-text search engine. This was
    UNDELIVERED SPEC before this fix: declared in spec §9, with no route
    parameter, no argument here, and no client method at all. The pattern
    (`Column.ilike(f"%{q}%")`, no manual wildcard-escaping of the caller's
    own `%`/`_`) mirrors the two existing precedents in this codebase --
    `inventory.api`'s software-name search and `security_logs.api`'s
    username search -- rather than inventing a third convention."""
    stmt = (
        _matching_tickets(
            org_id,
            type=type,
            status=status,
            queue_id=queue_id,
            assigned_to=assigned_to,
            requester_id=requester_id,
            device_id=device_id,
            client_id=client_id,
            priority=priority,
            q=q,
            unassigned=unassigned,
            open_only=open_only,
        )
    )
    if sort == "due":
        # Outer join so tickets without a clock stay in the list (sorted
        # last). `org_id` on the join because `ticket_sla.ticket_id` is a
        # plain FK with no org check.
        stmt = stmt.outerjoin(
            TicketSla, and_(TicketSla.ticket_id == Ticket.id, TicketSla.org_id == org_id)
        ).order_by(next_due_clause().asc().nulls_last())
    stmt = stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_tickets(
    session: AsyncSession,
    org_id: UUID,
    *,
    type: str | None = None,
    status: str | None = None,
    queue_id: UUID | None = None,
    assigned_to: UUID | None = None,
    requester_id: UUID | None = None,
    device_id: UUID | None = None,
    client_id: UUID | None = None,
    priority: str | None = None,
    q: str | None = None,
    unassigned: bool = False,
    open_only: bool = False,
) -> int:
    """How many tickets match, BEFORE `limit`/`offset` (closeout rec. 10).

    The route returned a bare list, so a console holding a full page could not
    tell "this is the queue" from "this is the first 200 of it" -- auditor A's
    finding, and the reason `TicketListPage` refused to render "showing X of
    Y" and said in a comment that there was no Y.

    IT TAKES THE SAME FILTERS AND SHARES THE SAME BUILDER as `list_tickets`,
    which is not tidiness: a count that drifted from the page beside it would
    be a number contradicting the rows under it, and every filter the route
    forwards has to reach both or the total silently answers a different
    question.
    """
    stmt = _matching_tickets(
        org_id,
        type=type,
        status=status,
        queue_id=queue_id,
        assigned_to=assigned_to,
        requester_id=requester_id,
        device_id=device_id,
        client_id=client_id,
        priority=priority,
        q=q,
        unassigned=unassigned,
        open_only=open_only,
    )
    return int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )


def _matching_tickets(
    org_id: UUID,
    *,
    type: str | None,
    status: str | None,
    queue_id: UUID | None,
    assigned_to: UUID | None,
    requester_id: UUID | None,
    device_id: UUID | None,
    client_id: UUID | None,
    priority: str | None,
    q: str | None,
    unassigned: bool = False,
    open_only: bool = False,
) -> Select[tuple[Ticket]]:
    """The filtered set, unordered and unpaged -- the one predicate the page
    and its total are both built from.

    The `org_id` predicate is explicit and is not decoration: RLS scopes this
    table under `org_session`, and invariant #5 asks for both. It is pinned
    where RLS is not the thing rejecting, in `tests/test_service_desk_api.py`'s
    `test_the_ticket_count_is_tenant_scoped_where_rls_is_not_the_thing_rejecting`,
    which fails with 2 == 1 when it is dropped.
    """
    stmt = select(Ticket).where(Ticket.org_id == org_id)
    if type is not None:
        stmt = stmt.where(Ticket.type == type)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if queue_id is not None:
        stmt = stmt.where(Ticket.queue_id == queue_id)
    if assigned_to is not None:
        stmt = stmt.where(Ticket.assigned_to == assigned_to)
    if requester_id is not None:
        stmt = stmt.where(Ticket.requester_id == requester_id)
    if device_id is not None:
        stmt = stmt.where(Ticket.device_id == device_id)
    if client_id is not None:
        stmt = stmt.where(Ticket.client_id == client_id)
    if priority is not None:
        stmt = stmt.where(Ticket.priority == priority)
    if q is not None:
        stmt = stmt.where(or_(Ticket.subject.ilike(f"%{q}%"), Ticket.description.ilike(f"%{q}%")))
    if unassigned:
        stmt = stmt.where(Ticket.assigned_to.is_(None))
    if open_only:
        stmt = stmt.where(Ticket.status.not_in(sorted(TERMINAL_STATUSES)))
    return stmt


# ---------------------------------------------------------------------------
# update_ticket
# ---------------------------------------------------------------------------


async def update_ticket(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    ticket_id: UUID,
    fields: dict[str, Any],
    *,
    actor_type: Literal["user", "system"] = "user",
) -> Ticket:
    """Apply the SUPPLIED fields to a ticket, org-scoped-loaded (spec §5).

    `actor_type` mirrors `transitions.change_status`'s system-actor path
    (task 8, spec §1): keyword-only with a `"user"` default, so every
    existing positional caller (the PATCH route, tests) is unchanged. The
    same validation pairing `change_status` enforces applies here -- an
    unknown `actor_type`, a `"system"` actor carrying an `actor_id`, and a
    `"user"` actor with `actor_id=None` are all rejected before the ticket
    is even loaded.

    Passes, deliberately not interleaved -- the same discipline
    `budget.expenses.update_expense` uses:

    0. Reject any key that is not in `TICKET_EDITABLE_FIELDS` -- naming the
       dedicated service for the three that have one (`queue_id`/
       `assigned_to` -> `transitions.assign_ticket`, `status` ->
       `transitions.change_status`). Runs before everything else, so a
       refused PATCH mutates nothing, not even its editable half. See
       `_reject_non_editable_fields`.
    1. Reject an explicit `null` on any `NON_NULLABLE_TICKET_FIELDS` member up
       front -- before the row is even loaded.
    2. Load the row, then reject the edit outright if it is terminal for
       editing (`lifecycle.TERMINAL_STATUSES`, spec §3.3).
    3. Resolve every supplied cross-reference (`category_id`, `device_id`)
       org-scoped. Nothing is `setattr`'d on the tracked `Ticket` in this
       pass, so a cross-org reference leaves the row untouched. A supplied
       `category_id` also re-checks the create-time
       type/category guard (spec §2's `data.type != category.type` rejection
       in `create_ticket`) against the ticket's own (immutable) `type` --
       `type` cannot move with the ticket, so re-pointing `category_id` alone
       must not be able to strand a `type="incident"` ticket on a
       `type="request"` category or vice versa. Enforcing this only in
       `create_ticket` and not here would leave the other write path free to
       violate the exact invariant the guard exists for.
    4. Apply every resolved value in one pass, tracking which field names
       actually changed (for the audit event -- names only, never subject or
       description values, spec §8.3).
    """
    if actor_type not in ("user", "system"):
        raise TicketValidationError(
            tr(
                "unknown actor_type: {actor_type}",
                actor_type=repr(actor_type),
            )
        )
    if actor_type == "system" and actor_id is not None:
        raise TicketValidationError(tr("actor_type='system' updates must not carry an actor_id"))
    if actor_type == "user" and actor_id is None:
        raise TicketValidationError(tr("actor_type='user' updates require an actor_id"))

    _reject_non_editable_fields(fields)
    _reject_null_for_non_nullable(fields, NON_NULLABLE_TICKET_FIELDS)
    ticket = await get_ticket(session, org_id, ticket_id)

    _reject_if_terminal(ticket)

    # Pass 3: resolve cross-references. `None` on a nullable ref clears it; a
    # supplied id is resolved org-scoped.
    resolved: dict[str, Any] = {}
    if "category_id" in fields:
        category = await resolve_category(session, org_id, fields["category_id"])
        if ticket.type != category.type:
            raise TicketValidationError(
                tr(
                    "ticket type {type} does not match category type {category_type}",
                    type=type_quoted(ticket.type),
                    category_type=type_quoted(category.type),
                )
            )
        resolved["category_id"] = category.id
    if "device_id" in fields:
        if fields["device_id"] is None:
            resolved["device_id"] = None
        else:
            device = await _resolve_device(session, org_id, fields["device_id"])
            # A ticket is its device's client's (spec §2.4); a ticket of X
            # cannot take a device of Y. Moving the ticket is its own act
            # (Task 3), and a ticket with a device cannot move (C9).
            if device.client_id != ticket.client_id:
                raise TicketClientError(tr("the ticket's client is not its device's client"))
            resolved["device_id"] = device.id

    # Pass 4: apply everything. Every key in `fields` is guaranteed to be in
    # TICKET_EDITABLE_FIELDS by pass 0, so this loop can no longer skip a
    # key the caller supplied (spec §9's module docstring).
    changed_fields = []
    for field in TICKET_EDITABLE_FIELDS:
        if field not in fields:
            continue
        new_value = resolved.get(field, fields[field])
        if getattr(ticket, field) != new_value:
            changed_fields.append(field)
        setattr(ticket, field, new_value)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="ticket.updated",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=ticket.client_id,
        # Names only, never subject/description values -- spec §8.3.
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()

    # SLA re-triage (SLA spec §0.3, §0.4, §3). A priority change re-resolves
    # the policy and recomputes both running deadlines FROM TICKET START, so a
    # ticket filed `normal` and raised to `urgent` at hour five comes out
    # already breached -- intended, and `policy_source = 'retriage'` is what
    # makes it attributable (§0.3).
    #
    # **`actor_type == "user"` IS §0.4'S LOOP GUARD, in its structural form.**
    # `sla.breach._escalate` reaches this exact function with
    # `actor_type="system"` to bump a breached ticket's priority. If that call
    # recomputed, the recompute would derive a shorter deadline from the same
    # `created_at`, the ticket would breach again in the same second, escalate
    # again, and fire a burst of notifications until `urgent` capped it. The
    # guard is that escalation simply does not reach `recompute_for_retriage`
    # -- and the actor is the only thing at this call site that distinguishes
    # the two callers, because the FIELD and the VALUE are identical either
    # way (`{"priority": ...}`). §3 says resolution runs "on human re-triage
    # (a priority or queue change with a human actor)"; this is that sentence.
    #
    # Keyed on `changed_fields`, not on `"priority" in fields`: a PATCH that
    # re-sends the priority it already had changed nothing about the promise,
    # and recomputing there would restamp `policy_source`/`last_recomputed_at`
    # and re-derive deadlines for a no-op edit. A PATCH touching only
    # `subject` recomputes nothing at all.
    #
    # The QUEUE half of §3's "priority or queue change" is NOT here and must
    # not be: `queue_id` is refused by this function (see
    # `_reject_non_editable_fields`) and moves only through
    # `transitions.assign_ticket`, which carries its own copy of this hook.
    if actor_type == "user" and "priority" in changed_fields:
        async with sla_misconfiguration_is_not_fatal("recompute_for_retriage", ticket.id):
            await recompute_for_retriage(session, org_id, ticket)
    return ticket


# ---------------------------------------------------------------------------
# move_ticket (MSP clients spec §2.4, §3.2; plan 2026-10-26 Task 3)
# ---------------------------------------------------------------------------

# The origins whose ticket belongs to something else's client, and so stays
# there: an alert's ticket is its device's alert's, and `integration` is the
# change adapter's (`adapters.push_change`, the only path that files one) --
# a change's ticket is the change's client's (C9's reasoning, research-1 §E6).
_ORIGINS_THAT_STAY: frozenset[str] = frozenset({"alert", "integration"})


async def _linked_to_an_alert(session: AsyncSession, org_id: UUID, ticket_id: UUID) -> bool:
    """Whether a health alert, or one of its actions, still names this ticket
    (research-1 §E6): its client is the alert's device's."""
    return bool(
        (
            await session.execute(
                select(
                    exists().where(HealthAlert.org_id == org_id, HealthAlert.ticket_id == ticket_id)
                    | exists().where(
                        HealthAlertAction.org_id == org_id,
                        HealthAlertAction.ticket_id == ticket_id,
                    )
                )
            )
        ).scalar_one()
    )


async def move_ticket(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    ticket_id: UUID,
    *,
    to_client_id: UUID,
) -> Ticket:
    """Move a ticket to another client (spec §2.4) -- the normal way out of
    "Sin asignar".

    The session must see BOTH clients (the route opens it so):
    `its_client_to_children` is SECURITY INVOKER and moves the five child
    tables in the same statement, and RLS must let it read the old client's
    rows and write them in the new one.

    Refused, before anything changes:
    * a destination the session cannot see (404), an archived one (409), or
      "Sin asignar" (422: tickets reach it only by email, C10);
    * a ticket with a device (409, C9): a device's client never changes and
      the ticket must agree with it -- take the device off, then move;
    * a ticket an alert or a change opened, or one an alert still links to
      (409, research-1 §E6): it belongs to that alert's or change's client;
    * a ticket whose requester is a contact of another client (422,
      research-1 §E2): its emailed replies would otherwise comment on a ticket
      of a client the contact is not one of.

    An assignee who cannot read tickets of the destination is taken off it --
    a ticket is never left with someone who cannot open it -- and the audit
    names them. The SLA is re-resolved among the destination's policies
    (D2), as a human re-triage: `policy_source = 'retriage'`.

    The destination is loaded first, as every client a write is filed under
    is: `client_for_new_rows`'s lock is a transaction's first (its module
    docstring). Then the ticket is held (`hold_ticket_client`, then FOR
    UPDATE) so no comment or attachment in flight is left behind.
    """
    try:
        destination = await client_for_new_rows(session, org_id=org_id, client_id=to_client_id)
    except ClientNotFound:
        raise TicketNotFoundError(tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise TicketConflictError(exc.detail) from None
    if destination.kind == CLIENT_KIND_UNASSIGNED:
        raise TicketClientError(tr("tickets reach Unassigned only by email"))

    await hold_ticket_client(session, ticket_id, moving=True)
    ticket = (
        await session.execute(
            select(Ticket)
            .where(Ticket.org_id == org_id, Ticket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise TicketNotFoundError(tr("ticket {ticket_id} not found", ticket_id=ticket_id))
    if ticket.client_id == to_client_id:
        return ticket
    if ticket.device_id is not None:
        raise TicketConflictError(
            tr("remove the ticket's device before moving it to another client")
        )
    if (
        ticket.source_alert_id is not None
        or ticket.origin in _ORIGINS_THAT_STAY
        or await _linked_to_an_alert(session, org_id, ticket.id)
    ):
        raise TicketConflictError(
            tr("a ticket opened by an alert or a change stays with its client")
        )
    if ticket.requester_id is not None:
        _requester_belongs(await resolve_user(session, org_id, ticket.requester_id), to_client_id)

    # Taken off the ticket: an assignee who cannot read the destination's.
    dropped = ticket.assigned_to
    if dropped is not None and await has_permission(
        session, dropped, "ticket.read", client_id=to_client_id
    ):
        dropped = None
    source = ticket.client_id
    ticket.client_id = to_client_id
    if dropped is not None:
        ticket.assigned_to = None
    await session.flush()

    # A move changes which policies the ticket resolves among (D2): the same
    # human re-triage `update_ticket` and `assign_ticket` run, under the same
    # guard -- a broken calendar may cost the ticket its SLA, never the move.
    async with sla_misconfiguration_is_not_fatal("recompute_for_retriage", ticket.id):
        await recompute_for_retriage(session, org_id, ticket)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.moved",
        target_type="ticket",
        target_id=str(ticket.id),
        client_id=to_client_id,
        metadata={
            "from": str(source),
            "to": str(to_client_id),
            "unassigned": str(dropped) if dropped is not None else None,
        },
    )
    await session.flush()
    return ticket
