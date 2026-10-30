"""Real backend for change management's `ChangeTicketPort` (spec §7.1).

Phase 9 shipped that port with a `NullChangeTicketAdapter` that records intent
and performs no network call, explicitly awaiting a backend. This is it.

Additive by design: change management's reviewed core is not modified, and the
null adapter remains the default until an operator sets
ITS_CHANGE_TICKET_BACKEND, so an existing deployment's behavior does not change
underneath it. `app.modules.change_mgmt.ticketing.get_ticket_adapter` is the
only place that needs to learn about this adapter -- everything else in
change management (the state machine, `transition_change`, `decide_change`)
already calls the port through the `ChangeTicketPort` interface and never
imports an adapter directly (docs/modules/change-mgmt/spec.md §5).

**What "opens a ticket" means here.** `push_change` creates a genuine
`type="incident"` ticket (spec §7.1: "opens a genuine internal ticket") in
the org's default queue -- concretely, the `"other"` incident category from
`refdata.DEFAULT_CATEGORIES`, whose `default_queue_id` resolves to the
`"helpdesk"` queue. `"other"` is used deliberately rather than inventing a
sixth, change-specific category: it is already the generic incident
catch-all a fresh org gets for free (`ensure_default_categories` is called
first, so an org that has never touched Service Desk still gets one), it
does not require approval, and a change is not one of the five taxonomy
buckets (`hardware`/`software`/`network`/`access`) Service Desk's own seed
already names.

**The requester and the acting user are the same person: `change.
created_by`.** The port's `push_change`/`update_change` signatures carry no
separate `actor_id` (`ticketing.ChangeTicketPort`) -- the only human
reference available is the change's own author, so the resulting ticket is
both requested by and attributed to them. This is a deliberate, narrow
reading of "requester" for a system-initiated ticket open, not a claim that
the change's author is literally filing a helpdesk ticket by hand.

**Linking is one-directional and lives in `change_external_ref`, not on the
ticket.** Spec §7.1: "A ticket does not own or drive a change record."
`push_change` writes exactly one `ChangeExternalRef(system="service_desk")`
row per change -- the same table the (still deferred) Odoo adapter would
have used, keyed by `(org_id, change_id, system)`, so a future Odoo adapter
and this one can coexist per-org without colliding rows.

**The known plumbing gap in `update_change`/`close_change` (read before
touching this file's tests).** `change_mgmt.service.transition_change`/
`decide_change` -- reviewed, unmodified code this module does not touch --
call `ticket.update_change(session, org_id=org_id, external_id="", change=
change)` and `ticket.close_change(session, org_id=org_id, external_id="")`
verbatim: `external_id` is ALWAYS the empty string at every pinned call
site in the current codebase, regardless of what `push_change` actually
returned. `update_change` still has a way to find the right ticket despite
that: it is handed `change`, so it falls back to a `change_external_ref`
lookup by `(org_id, change.id, system="service_desk")` whenever the passed
`external_id` is blank. `close_change` is handed no `change` at all --
only `external_id` -- so when that argument is blank (the only way it is
ever called from `change_mgmt.service` today) there is no way to resolve
which ticket to close, and this adapter no-ops rather than guessing. This
is a real, inherited limitation of the pinned call sites, not something
introduced here, and not something this task's brief authorizes fixing
(`change_mgmt`'s reviewed core is explicitly out of scope). Both methods
work correctly end-to-end when called directly with a real `external_id`
(what `push_change` returned) -- which is exactly how this module's own
tests exercise them, per the port's protocol.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChangeExternalRef, ChangeRequest
from app.modules.change_mgmt.state import ChangeConflictError
from app.modules.service_desk.errors import TicketConflictError, TicketValidationError
from app.modules.service_desk.lifecycle import FINAL_STATUSES
from app.modules.service_desk.refdata import ensure_default_categories
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.threads import add_comment
from app.modules.service_desk.tickets import create_ticket, get_ticket
from app.modules.service_desk.transitions import change_status

_SYSTEM = "service_desk"

# `ChangeRequest.risk` in {"low", "medium", "high"} (docs/modules/
# change-mgmt/spec.md §1) maps onto `TicketPriority`'s four-value domain
# ("low"/"normal"/"high"/"urgent") -- "medium" is the one value with no
# exact-name match, mapped to "normal" (schema.py's own default priority).
_RISK_TO_PRIORITY: dict[str, str] = {"low": "low", "medium": "normal", "high": "high"}

# `ticket_category.type="incident"`, so subject/description bounds mirror
# `service_desk.schema`'s `_MAX_SUBJECT`/`_MAX_BODY`. Those two names are
# module-private (a leading underscore) in `schema.py`, so they are not
# imported across the module boundary -- these are a second, independently
# maintained pair of numbers, kept in agreement by eye with schema.py, the
# same discipline `lifecycle.py`'s docstring already asks of
# `ck_ticket_status_matches_type`. A change's `title`/`description` have no
# bound at all in `change_mgmt.schema` today (a pre-existing gap in that
# module, not this adapter's to fix) -- truncating defensively here is what
# keeps an oversized change from turning into an unhandled Pydantic
# `ValidationError` (surfacing as a 500 through change management's own
# `_http_error`, which has no idea what a `TicketCreate` is) instead of a
# clean ticket subject/description.
_MAX_SUBJECT = 255
_MAX_DESCRIPTION = 20_000


async def _find_ref(
    session: AsyncSession, org_id: UUID, change_id: UUID
) -> ChangeExternalRef | None:
    """Org- and change-scoped load of this adapter's own external-ref row, if
    one exists. Never raises -- both `update_change` and `push_change`'s
    conflict-free-insert path treat "no row yet" as a legitimate state, not
    an error."""
    return (
        await session.execute(
            select(ChangeExternalRef).where(
                ChangeExternalRef.org_id == org_id,
                ChangeExternalRef.change_id == change_id,
                ChangeExternalRef.system == _SYSTEM,
            )
        )
    ).scalar_one_or_none()


def _parse_ticket_id(external_id: str) -> UUID | None:
    """`external_id` is a plain `str` at the port boundary (`ChangeTicketPort`
    is written for an arbitrary external system's opaque reference, not
    specifically a UUID) -- this adapter's own refs happen to be ticket
    UUIDs stringified, but a blank string (the pinned call sites' actual
    value, see module docstring) or garbage from some other system must not
    raise; both make ticket resolution fall through to the caller's other
    strategy (a `change_external_ref` lookup, or a no-op)."""
    if not external_id:
        return None
    try:
        return UUID(external_id)
    except ValueError:
        return None


class ServiceDeskChangeTicketAdapter:
    """Opens a genuine internal Service Desk ticket for a change (spec §7.1).

    Selected by `ITS_CHANGE_TICKET_BACKEND=service_desk`, the default
    (`app.modules.change_mgmt.ticketing.get_ticket_adapter`); `"null"` opts
    out.
    """

    async def push_change(
        self, session: AsyncSession, *, org_id: UUID, change: ChangeRequest
    ) -> str:
        """Create an `incident` ticket in the org's default queue (the
        `"other"` incident category's `default_queue_id`, seeded lazily by
        `ensure_default_categories` if this is the org's first-ever Service
        Desk touch) and record the link in `change_external_ref`. Returns
        the created ticket's id, stringified, as the external ref.

        The ticket is the CHANGE'S CLIENT'S (MSP clients plan 2026-10-26 Task
        7): the client a technician who may submit the change can see. An
        archived client takes no new ticket (`client_for_new_rows`); that is
        the change module's 409, not a ticket error escaping as a 500."""
        categories = await ensure_default_categories(session, org_id, change.created_by)
        category = next(c for c in categories if c.key == "other")

        try:
            ticket = await create_ticket(
                session,
                org_id,
                change.created_by,
                TicketCreate(
                    type="incident",
                    category_id=category.id,
                    subject=f"Change {change.type}: {change.title}"[:_MAX_SUBJECT],
                    description=change.description[:_MAX_DESCRIPTION],
                    priority=_RISK_TO_PRIORITY.get(change.risk, "normal"),
                    requester_id=change.created_by,
                ),
                # `origin="integration"` (whole-branch security review,
                # finding A3), not `"technician"`: this ticket is opened by the
                # system reacting to a LATER, possibly much-later, change
                # transition -- not by `change.created_by` sitting down and
                # filing a helpdesk ticket. `requester_id`/`created_by` still
                # carry that person's real id (a genuine requester); only
                # `create_ticket`'s CREATE-action attribution
                # (`ticket_status_history`/`ticket.created`'s `actor_type`)
                # becomes `"system"` -- see `tickets.py`'s
                # `_SYSTEM_ATTRIBUTED_ORIGINS`.
                origin="integration",
                client_id=change.client_id,
            )
        except TicketConflictError as exc:
            raise ChangeConflictError(str(exc)) from None

        session.add(
            ChangeExternalRef(
                org_id=org_id,
                change_id=change.id,
                system=_SYSTEM,
                external_id=str(ticket.id),
            )
        )
        await session.flush()
        return str(ticket.id)

    async def update_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str, change: ChangeRequest
    ) -> None:
        """Leave a system comment on the linked ticket noting the change's
        current state. Resolves the ticket from `external_id` when it is
        usable; falls back to a `change_external_ref` lookup by `change.id`
        otherwise (module docstring's plumbing-gap note) -- `change` is
        always available here, unlike `close_change`. A ticket that cannot
        be resolved at all (no ref ever written, or the ticket itself is
        gone) is a silent no-op: this method has no way to report failure
        back to `change_mgmt.service`, which does not check its return
        value."""
        ticket_id = _parse_ticket_id(external_id)
        if ticket_id is None:
            ref = await _find_ref(session, org_id, change.id)
            if ref is None:
                return
            ticket_id = _parse_ticket_id(ref.external_id)
            if ticket_id is None:
                return

        try:
            ticket = await get_ticket(session, org_id, ticket_id)
        except TicketValidationError:
            return

        try:
            await add_comment(
                session,
                org_id,
                ticket.id,
                body=f"Change {change.title!r} is now {change.state!r}.",
                author_id=None,
                author_type="system",
            )
        except TicketValidationError:
            # The linked ticket is terminal for thread content
            # (`lifecycle.TERMINAL_STATUSES`) -- e.g. it was already
            # cancelled independently of the change. A status note failing
            # to attach is not a reason to raise out of a change-management
            # transition this adapter has no authority to block.
            return

    async def close_change(
        self, session: AsyncSession, *, org_id: UUID, external_id: str
    ) -> None:
        """Drive the linked ticket toward `resolved` -- never straight to
        `closed`, mirroring spec §7.2's Phase 15 alert-recovery convention
        ("recovery drives them to resolved... so a human still confirms
        that the underlying problem is genuinely gone"). No `change` object
        is available here (only `external_id`) -- see the module docstring:
        the pinned call site always passes `""`, so this is a no-op through
        the current `change_mgmt.service` wiring, and only takes effect when
        called directly with a real external id (as this module's own tests
        do, exercising the port's protocol)."""
        ticket_id = _parse_ticket_id(external_id)
        if ticket_id is None:
            return

        try:
            ticket = await get_ticket(session, org_id, ticket_id)
        except TicketValidationError:
            return

        # KNOWN FOLLOW-UP (whole-branch review round B found this, round C's
        # C4 records it operator-facing in deploy/README.md): this guard is
        # structurally DEAD given the if-chain below it. Deleting it changes
        # nothing -- driving a ticket already in FINAL_STATUSES or "closed"
        # through the two `if`s below still does nothing, because neither
        # `ticket.status == "new"` nor `ticket.status in ("open", "pending")`
        # can ever be true for those statuses either. It costs nothing to
        # leave (an early, explicit `return` reads better than "falls through
        # and no-ops anyway"), but a genuine mutation-verified pinning test
        # for it is not constructible as written. Revisit together with
        # `close_change`'s inertness fix (README's "opened tickets never
        # auto-resolve" bullet) rather than in isolation -- both live in the
        # same method and a real fix there may reshape this guard anyway.
        if ticket.status in FINAL_STATUSES or ticket.status == "closed":
            return  # already terminal or already resolved-and-closed

        try:
            if ticket.status == "new":
                ticket = await change_status(
                    session,
                    org_id,
                    ticket.id,
                    to_status="open",
                    actor_id=None,
                    actor_type="system",
                    reason="linked change closed",
                )
            if ticket.status in ("open", "pending"):
                await change_status(
                    session,
                    org_id,
                    ticket.id,
                    to_status="resolved",
                    actor_id=None,
                    actor_type="system",
                    reason="linked change closed",
                )
        except TicketValidationError:
            # An illegal transition here (e.g. a race with a concurrent
            # manual status change) must not propagate out of a change's own
            # close -- this adapter has no authority to block that.
            return
