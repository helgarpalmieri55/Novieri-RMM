"""The ticket state machine (spec.md §3).

This is the **sole** authority on what state a ticket may be in and what
transitions between states are legal. No other file in this module -- the
service layer, the schema layer, the tests -- re-derives any part of this
logic; they import it. Spec §3.3 names the failure mode this guards against
directly: the Budget module produced two Critical double-counts because two
files each implemented "what state can this be in" and disagreed.

Pure logic: no database, no session, no I/O, no imports from `models`. The
DB check constraint `ck_ticket_status_matches_type`
(`backend/migrations/versions/0023_service_desk.py`) pins the same
`(type, status)` pairs as `INCIDENT_STATUSES`/`REQUEST_STATUSES` below --
those two sources must be kept in agreement by eye; a divergence surfaces
as an `IntegrityError` 500 on a transition the service layer believed legal.

The transition table is modeled as an explicit nested dict, not derived from
`TERMINAL_STATUSES` or any other rule, because the machine has one
deliberate exception to "terminal states have no outgoing transitions":
`closed` is reopenable (`closed -> open` / `closed -> in_progress`) while
`cancelled` and `rejected` are not. A reader must be able to see the whole
machine at a glance and compare it against the migration's CHECK constraint;
a clever derivation would hide that exception instead of showing it.
"""

from app.core.i18n import tr
from app.core.ticket_words import status_quoted, type_word
from app.modules.service_desk.errors import TicketValidationError

# Spec §3.1/§3.3. Must match `ck_ticket_status_matches_type`'s incident
# branch in migrations/versions/0023_service_desk.py exactly.
INCIDENT_STATUSES: frozenset[str] = frozenset(
    {"new", "open", "pending", "resolved", "closed", "cancelled"}
)

# Spec §3.2/§3.3. Must match `ck_ticket_status_matches_type`'s request
# branch in migrations/versions/0023_service_desk.py exactly.
REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        "new",
        "pending_approval",
        "in_progress",
        "fulfilled",
        "closed",
        "rejected",
        "cancelled",
    }
)

# Spec §3.3 distinguishes TWO notions of terminal, and conflating them is a bug:
# `cancelled` and `rejected` are finally terminal (nothing leaves them, ever),
# while `closed` is terminal for EDITING but deliberately reopenable
# (`closed -> open` for an incident, `closed -> in_progress` for a request).
#
# So use this set to gate FIELD EDITS AND COMMENTS only.
#
# NEVER gate the status-transition path on it. A `if status in
# TERMINAL_STATUSES: reject()` guard on the transition endpoint would silently
# break every legitimate reopen -- the ticket would be closed forever with no
# error anyone could act on. Call `assert_transition` there instead: it already
# knows which reopens are legal and which terminal states are truly final.
TERMINAL_STATUSES: frozenset[str] = frozenset({"closed", "cancelled", "rejected"})

# The subset that nothing ever leaves. Provided so a caller that genuinely
# means "this can never move again" does not have to reach for the broader set
# above and get `closed` by accident.
FINAL_STATUSES: frozenset[str] = frozenset({"cancelled", "rejected"})

# The whole machine, in one place. Keyed by ticket type, then by from-status,
# to the set of to-statuses that transition legally allows. A from-status
# missing from a type's map (or mapped to an empty set) has no outgoing
# transitions.
_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "incident": {
        # spec §3.1
        "new": frozenset({"open", "cancelled"}),
        "open": frozenset({"pending", "resolved", "cancelled"}),
        # `pending -> resolved` matters: the third party comes back and the fix
        # lands directly from there. Without it a technician who parked a
        # ticket on someone else has to bounce it through `open` purely to
        # satisfy the state machine, which makes the history lie about what
        # happened. `pending` is an optional detour off `open`, not a stage
        # every incident must pass through (spec §3.1).
        "pending": frozenset({"open", "resolved", "cancelled"}),
        # `resolved -> open` is the reopen path; `resolved -> cancelled`
        # is required because `resolved` is non-terminal ("any non-terminal
        # -> cancelled", spec §3.1).
        "resolved": frozenset({"open", "closed", "cancelled"}),
        # `closed` is terminal but deliberately reopenable (spec §3.1/§3.3).
        "closed": frozenset({"open"}),
        # terminal, no outgoing transitions (spec §3.3).
        "cancelled": frozenset(),
    },
    "request": {
        # spec §3.2. `new -> pending_approval` and `new -> in_progress` are
        # both reachable per the diagram, even though `initial_status`
        # below never returns `"new"` itself -- creation lands directly on
        # whichever of the two applies (spec §3.2's second diagram line).
        "new": frozenset({"pending_approval", "in_progress", "cancelled"}),
        "pending_approval": frozenset({"in_progress", "rejected", "cancelled"}),
        "in_progress": frozenset({"fulfilled", "cancelled"}),
        # `fulfilled -> in_progress` is the reopen path; `fulfilled ->
        # cancelled` is required because `fulfilled` is non-terminal.
        "fulfilled": frozenset({"in_progress", "closed", "cancelled"}),
        # `closed` is terminal but deliberately reopenable (spec §3.2/§3.3).
        "closed": frozenset({"in_progress"}),
        # terminal, no outgoing transitions (spec §3.3).
        "rejected": frozenset(),
        "cancelled": frozenset(),
    },
}


def initial_status(ticket_type: str, *, requires_approval: bool) -> str:
    """The status a newly created ticket of `ticket_type` starts in.

    Incidents always start `new`. Requests skip straight to `in_progress`
    unless the category requires approval, in which case they start
    `pending_approval` (spec §3.2, §4).
    """
    if ticket_type == "incident":
        return "new"
    if ticket_type == "request":
        return "pending_approval" if requires_approval else "in_progress"
    raise TicketValidationError(
        tr(
            "unknown ticket type: {ticket_type}",
            ticket_type=repr(ticket_type),
        )
    )


def can_transition(ticket_type: str, from_status: str, to_status: str) -> bool:
    """Whether `from_status -> to_status` is a legal transition for
    `ticket_type`. Unknown ticket types, or a `from_status` with no
    outgoing transitions, simply yield `False` rather than raising --
    callers that need a hard failure use `assert_transition` instead.
    """
    by_status = _TRANSITIONS.get(ticket_type, {})
    return to_status in by_status.get(from_status, frozenset())


def assert_transition(ticket_type: str, from_status: str, to_status: str) -> None:
    """Raise `TicketValidationError` naming both statuses if the transition
    is not legal. Callers that must reject an illegal transition, rather
    than merely query one, use this."""
    if not can_transition(ticket_type, from_status, to_status):
        raise TicketValidationError(
            tr(
                "illegal {ticket_type} transition: {from_status} -> {to_status}",
                ticket_type=type_word(ticket_type),
                from_status=status_quoted(from_status),
                to_status=status_quoted(to_status),
            )
        )


# The transitions that resolve a ticket in one act, by type and current
# status. A new ticket is opened first, because the machine has no direct
# `new -> resolved` edge. `pending_approval` is absent on purpose: only an
# approval decision may move a request out of it.
RESOLVE_PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "incident": {
        "new": ("open", "resolved"),
        "open": ("resolved",),
        "pending": ("resolved",),
    },
    "request": {
        "new": ("in_progress", "fulfilled"),
        "in_progress": ("fulfilled",),
    },
}


def resolve_path(ticket_type: str, status: str) -> tuple[str, ...] | None:
    """The transitions that resolve a ticket from `status`, or None when it
    cannot be resolved from there."""
    return RESOLVE_PATHS.get(ticket_type, {}).get(status)
