"""Request approval and rejection (docs/modules/service-desk/spec.md §4,
§8.2, §8.3).

Covers, in order: `create_ticket` (task 9's decision, `tickets.py`) creates
the `ticket_approval` row -- in `pending` -- exactly when the category
requires approval, and creates none when it does not; approving moves
`pending_approval -> in_progress` and stamps the row (`decided_by`,
`decided_at`, `state="approved"`); rejecting moves to `rejected` and
**requires a non-empty reason**, empty/whitespace-only included; **the
requester may not approve their own request, even holding `ticket.approve`**
(spec §4's whole reason for splitting `approve` from `write`, §8.2) -- this
is the single most important test in this file; deciding twice raises and
the second call never touches the ticket; deciding an incident raises;
deciding a request whose category does not require approval raises (no
`ticket_approval` row is ever created for it, so `decide_approval`'s row
load finds nothing and falls back to the category's `requires_approval`
flag for a distinct, readable error -- this cannot be reduced to "the row
is missing" alone); flipping a category's `requires_approval` off after a
ticket is already `pending_approval` under it does NOT strand that ticket
(the row, once it exists, is authoritative over the category's live flag --
the fix for the review's T1 finding); a second `decide_approval` on an
already-decided approval raises even when the ILLEGAL-TRANSITION guard in
`transitions.change_status` would not by itself reject the call (the fix
for the review's T2 finding -- the two `deciding_twice` tests above pass
even with `approvals.py`'s own state check deleted, because by then
`ticket.status` has already left `pending_approval`; this isolates the
state check on its own); `ticket_category.approver_role_id`, when set,
restricts who may decide, checked org-scoped on a raw BYPASSRLS admin
session (never inside `org_session`, where RLS alone would mask a missing
`org_id` predicate and prove nothing about the explicit one in
`_actor_holds_role`); both decisions are audited exactly once
(`ticket.approved` / `ticket.rejected`, identifiers only in `meta`, never
the free-text `reason`) and each writes exactly one new
`ticket_status_history` row; and a ticket from another org is rejected via
`get_ticket`'s own tenancy guard, exercised again here on the new call
path.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    AuditEvent,
    Role,
    Ticket,
    TicketApproval,
    TicketCategory,
    TicketStatusHistory,
    User,
    UserRole,
)
from app.modules.service_desk.approvals import decide_approval
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.refdata import create_category, create_queue, update_category
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.tickets import create_ticket

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_service_desk_transitions.py's shapes -- not
# imported across test modules, per convention)
# ---------------------------------------------------------------------------


async def _org_with_user(api_env, slug: str = "sd-approvals") -> tuple[UUID, UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _second_user(api_env, org_id: UUID, *role_names: str) -> UUID:
    sub = await api_env.create_user(org_id, *role_names)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        return (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()


async def _category(
    session,
    org_id,
    actor_id,
    *,
    key: str = "access_request",
    requires_approval: bool = True,
    approver_role_id: UUID | None = None,
):
    return await create_category(
        session,
        org_id,
        actor_id,
        key=key,
        name=key.title(),
        type="request",
        requires_approval=requires_approval,
        approver_role_id=approver_role_id,
    )


async def _create_request(
    session,
    org_id,
    requester_id,
    actor_id,
    *,
    requires_approval: bool = True,
    approver_role_id: UUID | None = None,
    key: str = "access_request",
):
    """Creates a queue, a `type="request"` category with the given approval
    settings, and a ticket under it -- `requester_id` and `actor_id`
    (`created_by`) are separate on purpose, so self-approval tests can hold
    them equal and every other test can hold them apart."""
    queue = await create_queue(session, org_id, actor_id, key="requests", name="Requests")
    category = await _category(
        session,
        org_id,
        actor_id,
        key=key,
        requires_approval=requires_approval,
        approver_role_id=approver_role_id,
    )
    ticket = await create_ticket(
        session,
        org_id,
        actor_id,
        TicketCreate(
            type="request",
            category_id=category.id,
            queue_id=queue.id,
            subject="Need VPN access",
            requester_id=requester_id,
        ),
    )
    return ticket, category


async def _create_incident(session, org_id, actor_id):
    queue = await create_queue(session, org_id, actor_id, key="helpdesk", name="Helpdesk")
    category = await create_category(
        session,
        org_id,
        actor_id,
        key="hardware",
        name="Hardware",
        type="incident",
    )
    return await create_ticket(
        session,
        org_id,
        actor_id,
        TicketCreate(
            type="incident",
            category_id=category.id,
            queue_id=queue.id,
            subject="Laptop won't boot",
            requester_id=actor_id,
        ),
    )


async def _approval_row(session, org_id, ticket_id) -> TicketApproval | None:
    return (
        await session.execute(
            select(TicketApproval).where(
                TicketApproval.org_id == org_id, TicketApproval.ticket_id == ticket_id
            )
        )
    ).scalar_one_or_none()


async def _history_rows(session, org_id, ticket_id) -> list[TicketStatusHistory]:
    return list(
        (
            await session.execute(
                select(TicketStatusHistory)
                .where(
                    TicketStatusHistory.org_id == org_id,
                    TicketStatusHistory.ticket_id == ticket_id,
                )
                .order_by(TicketStatusHistory.id)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# create_ticket -- the row this task's brief assumes exists (task 9's
# decision: create_ticket creates it, decide_approval only stamps it)
# ---------------------------------------------------------------------------


async def test_create_ticket_creates_pending_approval_row_when_category_requires_it(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, user_id, user_id, requires_approval=True
        )
        approval = await _approval_row(session, org_id, ticket.id)

    assert ticket.status == "pending_approval"
    assert approval is not None
    assert approval.state == "pending"
    assert approval.decided_by is None
    assert approval.decided_at is None
    assert approval.reason == ""


async def test_create_ticket_creates_no_approval_row_when_category_does_not_require_it(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, user_id, user_id, requires_approval=False
        )
        approval = await _approval_row(session, org_id, ticket.id)

    assert ticket.status == "in_progress"
    assert approval is None


# ---------------------------------------------------------------------------
# decide_approval -- approve
# ---------------------------------------------------------------------------


async def test_approve_moves_pending_approval_to_in_progress_and_stamps_row(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )

        decided = await decide_approval(
            session, org_id, approver_id, ticket.id, approve=True
        )

    assert ticket.status == "in_progress"
    assert decided.state == "approved"
    assert decided.decided_by == approver_id
    assert decided.decided_at is not None
    assert decided.reason == ""


async def test_approve_does_not_require_a_reason(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        decided = await decide_approval(session, org_id, approver_id, ticket.id, approve=True)

    assert decided.state == "approved"


# ---------------------------------------------------------------------------
# decide_approval -- reject
# ---------------------------------------------------------------------------


async def test_reject_moves_to_rejected_and_stamps_row(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )

        decided = await decide_approval(
            session, org_id, approver_id, ticket.id, approve=False, reason="policy violation"
        )

    assert ticket.status == "rejected"
    assert decided.state == "rejected"
    assert decided.decided_by == approver_id
    assert decided.decided_at is not None
    assert decided.reason == "policy violation"


@pytest.mark.parametrize("reason", ["", "   "])
async def test_reject_requires_a_non_empty_reason(api_env, reason: str) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        ticket_id = ticket.id

        with pytest.raises(TicketValidationError):
            await decide_approval(
                session, org_id, approver_id, ticket_id, approve=False, reason=reason
            )

        session.expire(ticket)
        reloaded = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        approval = await _approval_row(session, org_id, ticket_id)

    assert reloaded.status == "pending_approval"
    assert approval is not None
    assert approval.state == "pending"


# ---------------------------------------------------------------------------
# decide_approval -- self-approval prohibition (spec §4, §8.2). This is the
# entire reason `ticket.approve` is separate from `ticket.write`.
# ---------------------------------------------------------------------------


async def test_requester_cannot_approve_their_own_request(
    api_env, admin_engine: AsyncEngine
) -> None:
    """The requester holds `ticket.approve` itself here (via `org-admin`,
    which grants all five ticket permissions) -- and is STILL rejected. If
    the requester could self-approve while holding the permission, the
    `ticket.approve`/`ticket.write` split (spec §8.2) would be decorative."""
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )

        with pytest.raises(TicketValidationError) as exc:
            await decide_approval(session, org_id, requester_id, ticket.id, approve=True)
        assert "own request" in str(exc.value).lower()

    # And the ticket did not move -- a fresh session, no identity map to be
    # fooled by (the brief's other sanctioned option besides
    # `session.expire()`).
    async with AsyncSession(admin_engine, expire_on_commit=False) as fresh:
        after = (
            await fresh.execute(select(Ticket).where(Ticket.id == ticket.id))
        ).scalar_one()
        approval = await _approval_row(fresh, org_id, ticket.id)
    assert after.status == "pending_approval"
    assert approval is not None
    assert approval.state == "pending"


async def test_requester_cannot_reject_their_own_request_either(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )

        with pytest.raises(TicketValidationError) as exc:
            await decide_approval(
                session, org_id, requester_id, ticket.id, approve=False, reason="no thanks"
            )
        assert "own request" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# decide_approval -- deciding twice raises
# ---------------------------------------------------------------------------


async def test_deciding_twice_raises(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        ticket_id = ticket.id
        first = await decide_approval(session, org_id, approver_id, ticket.id, approve=True)
        assert first.state == "approved"

        with pytest.raises(TicketValidationError):
            await decide_approval(session, org_id, approver_id, ticket_id, approve=True)

        # The second, rejected call must not have touched anything either.
        # Capture the id BEFORE expiring -- an expired instance's own `.id`
        # attribute requires an awaited reload too, and a synchronous
        # attribute access outside an awaited call raises `MissingGreenlet`.
        session.expire(ticket)
        reloaded = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
    assert reloaded.status == "in_progress"


async def test_deciding_twice_raises_even_when_the_second_decision_differs(api_env) -> None:
    """An approved ticket cannot be flipped to rejected by a second call
    either -- `decide_approval` has no "revise" mode."""
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        await decide_approval(session, org_id, approver_id, ticket.id, approve=True)

        with pytest.raises(TicketValidationError):
            await decide_approval(
                session, org_id, approver_id, ticket.id, approve=False, reason="changed my mind"
            )


async def test_deciding_when_approval_row_already_decided_but_transition_still_legal_raises(
    api_env, admin_engine: AsyncEngine
) -> None:
    """T2 review finding: the two `test_deciding_twice_raises*` tests above
    pass even with `approvals.py`'s own `if approval.state != "pending":
    raise` block deleted, because by the time a SECOND `decide_approval`
    call runs, `ticket.status` has already left `pending_approval` --
    `transitions.change_status` independently rejects that second call as an
    illegal transition regardless of whether the state guard exists. That is
    a real confound: it gives false confidence about the exact guard this
    task was reviewed hardest on.

    This test isolates the state check from the transition check by
    constructing a state the other two tests never reach: the approval row
    says `state="approved"` (stamped directly here, on a raw admin session,
    bypassing `decide_approval` entirely) while `ticket.status` is left at
    `pending_approval` -- never moved, because `change_status` is never
    called. `pending_approval -> in_progress` is a LEGAL transition for a
    ticket sitting in `pending_approval`, so if the state guard were gone,
    `change_status` would raise nothing and `decide_approval` would silently
    re-decide an already-decided approval. This fails if and only if the
    state guard is gone -- verified by mutation testing (delete the guard,
    confirm this test fails while the two `deciding_twice` tests above still
    pass, restore, confirm the file is identical; reported in the task's fix
    report)."""
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        approval = await _approval_row(session, org_id, ticket.id)
        assert approval is not None
        approval.state = "approved"
        approval.decided_by = approver_id
        approval.decided_at = datetime.now(UTC)
        await session.flush()

        # Confirm the setup actually isolates what it claims to: the
        # transition pending_approval -> in_progress is still legal here --
        # `ticket.status` was never touched by the direct row stamp above.
        reloaded = (
            await session.execute(select(Ticket).where(Ticket.id == ticket.id))
        ).scalar_one()
        assert reloaded.status == "pending_approval"

        with pytest.raises(TicketValidationError) as exc:
            await decide_approval(session, org_id, approver_id, ticket.id, approve=True)
        assert "already decided" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# decide_approval -- deciding an incident, or a request category that does
# not require approval, both raise
# ---------------------------------------------------------------------------


async def test_deciding_an_incident_raises(api_env) -> None:
    """T3: this has the same confound T2's isolation test exists to catch --
    an incident category can never have `requires_approval=True`
    (`refdata._reject_incident_requires_approval`), so deleting the
    `ticket.type != "request"` check leaves the next check (no approval row,
    category does not require approval) raising anyway. This test asserts
    the OUTCOME (deciding an incident is rejected), not that specific guard;
    see `test_deciding_when_approval_row_already_decided_but_transition_still_legal_raises`
    below for the test that isolates a single guard precisely."""
    org_id, admin_id = await _org_with_user(api_env)
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, admin_id)

        with pytest.raises(TicketValidationError):
            await decide_approval(session, org_id, approver_id, ticket.id, approve=True)


async def test_decide_approval_rejects_incident_type_before_reaching_approval_row(
    api_env, admin_engine: AsyncEngine
) -> None:
    """B4 (whole-branch review, round B): isolates `decide_approval`'s own
    `ticket.type != "request"` guard from the fallback `test_deciding_an_
    incident_raises` above actually exercises. That test's incident has NO
    `ticket_approval` row (an incident category can never carry
    `requires_approval=True`), so even with the type guard deleted, the very
    NEXT check ("no row exists; category.requires_approval is False") raises
    anyway -- the type guard itself is unexercised.

    Insert a `ticket_approval` row against an incident directly on a raw
    admin session -- a shape `create_ticket` never produces on its own (it
    only ever inserts one when status lands on `pending_approval`, which no
    incident can reach) -- so `_load_approval` finds a real `pending` row and
    the "no row" fallback never fires either. With the type guard removed,
    `decide_approval` proceeds all the way to `change_status(..., to_status=
    "in_progress", _approval_decision=True)`, which `lifecycle.
    assert_transition` STILL refuses (`"in_progress"` is not a legal
    incident-status target from any incident status) -- so a mutation still
    raises `TicketValidationError`, just from a DIFFERENT guard with a
    DIFFERENT message. Asserting the type guard's own message text is what
    actually distinguishes the two (mutation-verified, see
    `.git/sdd/fix-B-report.md`)."""
    org_id, admin_id = await _org_with_user(api_env)
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_id, admin_id)
        foreign_approval = TicketApproval(
            org_id=org_id, ticket_id=ticket.id, state="pending",
        )
        session.add(foreign_approval)
        await session.flush()
        ticket_id = ticket.id

        with pytest.raises(TicketValidationError) as exc:
            await decide_approval(session, org_id, approver_id, ticket_id, approve=True)
        assert "only requests are approved" in str(exc.value).lower()

        session.expire(ticket)
        reloaded_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        reloaded_approval = await _approval_row(session, org_id, ticket_id)
    assert reloaded_ticket.status == "new"
    assert reloaded_approval is not None
    assert reloaded_approval.state == "pending"


async def test_deciding_a_request_whose_category_does_not_require_approval_raises(
    api_env,
) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=False
        )
        # No ticket_approval row exists for this ticket at all (asserted
        # above in the create_ticket tests) -- `decide_approval` attempts the
        # row load first (T1: the row is authoritative when it exists), finds
        # nothing, and only THEN falls back to the category's live
        # `requires_approval` flag to decide which error applies.
        assert await _approval_row(session, org_id, ticket.id) is None

        with pytest.raises(TicketValidationError):
            await decide_approval(session, org_id, approver_id, ticket.id, approve=True)


async def test_flipping_category_requires_approval_off_does_not_strand_a_pending_ticket(
    api_env,
) -> None:
    """T1 review finding: `refdata.update_category` places no restriction on
    flipping `requires_approval` off while tickets are outstanding under
    that category. Before the fix, `decide_approval` re-derived "does this
    need approval" from the category's CURRENT flag before ever loading the
    `ticket_approval` row -- so an admin's ordinary category edit here would
    have permanently stranded this ticket in `pending_approval`: `lifecycle`
    gives that status no other way out, and every later `decide_approval`
    would have raised "category does not require approval" before the row
    was even loaded.

    This creates a ticket under an approval-requiring category, flips the
    category's `requires_approval` off (an ordinary edit, unrelated to this
    ticket), then confirms BOTH decisions still work: approving one ticket
    and rejecting a second, freshly created one, are both still legal even
    though the category no longer requires approval for anything new."""
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        # Built by hand, not `_create_request` (which would try to create a
        # second "requests" queue and collide with `uq_ticket_queue_org_key`
        # on a second call) -- both tickets need to share the SAME category
        # so the flip below is one edit affecting both.
        queue = await create_queue(session, org_id, admin_id, key="requests", name="Requests")
        category = await _category(session, org_id, admin_id, requires_approval=True)

        ticket_to_approve = await create_ticket(
            session,
            org_id,
            admin_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need VPN access",
                requester_id=requester_id,
            ),
        )
        ticket_to_reject = await create_ticket(
            session,
            org_id,
            admin_id,
            TicketCreate(
                type="request",
                category_id=category.id,
                queue_id=queue.id,
                subject="Need admin access",
                requester_id=requester_id,
            ),
        )
        assert ticket_to_approve.status == "pending_approval"
        assert ticket_to_reject.status == "pending_approval"

        # An ordinary edit, unrelated to either ticket above -- and one
        # `update_category` has never restricted.
        await update_category(
            session, org_id, admin_id, category.id, {"requires_approval": False}
        )
        reloaded_category = (
            await session.execute(
                select(TicketCategory).where(TicketCategory.id == category.id)
            )
        ).scalar_one()
        assert reloaded_category.requires_approval is False

        approved = await decide_approval(
            session, org_id, approver_id, ticket_to_approve.id, approve=True
        )
        rejected = await decide_approval(
            session,
            org_id,
            approver_id,
            ticket_to_reject.id,
            approve=False,
            reason="no longer needed",
        )

    assert approved.state == "approved"
    assert ticket_to_approve.status == "in_progress"
    assert rejected.state == "rejected"
    assert ticket_to_reject.status == "rejected"


# ---------------------------------------------------------------------------
# decide_approval -- ticket_category.approver_role_id
# ---------------------------------------------------------------------------


async def test_deciding_without_the_required_role_raises(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    non_member_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        role = Role(org_id=org_id, name="access-approver")
        session.add(role)
        await session.flush()

        ticket, _category = await _create_request(
            session,
            org_id,
            requester_id,
            admin_id,
            requires_approval=True,
            approver_role_id=role.id,
        )

        with pytest.raises(TicketValidationError):
            await decide_approval(session, org_id, non_member_id, ticket.id, approve=True)


async def test_deciding_with_the_required_role_succeeds(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    member_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        role = Role(org_id=org_id, name="access-approver")
        session.add(role)
        await session.flush()
        session.add(UserRole(org_id=org_id, user_id=member_id, role_id=role.id))
        await session.flush()

        ticket, _category = await _create_request(
            session,
            org_id,
            requester_id,
            admin_id,
            requires_approval=True,
            approver_role_id=role.id,
        )

        decided = await decide_approval(session, org_id, member_id, ticket.id, approve=True)

    assert decided.state == "approved"


async def test_null_approver_role_id_lets_any_actor_decide(api_env) -> None:
    """spec §4: `approver_role_id` NULL means any `ticket.approve` holder may
    decide -- no membership check runs at all."""
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        assert category.approver_role_id is None

        decided = await decide_approval(session, org_id, approver_id, ticket.id, approve=True)
    assert decided.state == "approved"


async def test_approver_role_membership_is_org_scoped(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Spec §8.1 defence-in-depth: run on a raw BYPASSRLS admin session, not
    `org_session` -- inside `org_session`, RLS alone already hides the wrong
    org's `user_roles` row, so this would pass even with the explicit
    `UserRole.org_id == org_id` predicate deleted from `_actor_holds_role`.
    The grant below is deliberately mis-stamped with `org_id=org_b` for a
    role that belongs to `org_a` and an actor who is a real member of
    `org_b` -- exactly the row a missing predicate would incorrectly honor
    (matching only on `user_id`/`role_id`, which -- unlike most cross-org
    checks in this module -- would otherwise "work" by accident here, since
    role ids are unique; the predicate is what makes the grant's OWN org
    matter)."""
    org_a, admin_a = await _org_with_user(api_env, "sd-approvals-a")
    org_b, _admin_b = await _org_with_user(api_env, "sd-approvals-b")
    actor_id = await _second_user(api_env, org_b, "technician")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        role = Role(org_id=org_a, name="access-approver")
        session.add(role)
        await session.flush()
        session.add(UserRole(org_id=org_b, user_id=actor_id, role_id=role.id))
        await session.flush()

        requester_id = await _second_user(api_env, org_a, "technician")
        ticket, _category = await _create_request(
            session, org_a, requester_id, admin_a, requires_approval=True, approver_role_id=role.id
        )

        with pytest.raises(TicketValidationError):
            await decide_approval(session, org_a, actor_id, ticket.id, approve=True)


# ---------------------------------------------------------------------------
# decide_approval -- tenancy: a ticket from another org
# ---------------------------------------------------------------------------


async def test_decide_approval_ticket_from_another_org_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, admin_a = await _org_with_user(api_env, "sd-approvals-a")
    org_b, admin_b = await _org_with_user(api_env, "sd-approvals-b")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        requester_id = await _second_user(api_env, org_a, "technician")
        ticket, _category = await _create_request(
            session, org_a, requester_id, admin_a, requires_approval=True
        )

        with pytest.raises(TicketNotFoundError):
            await decide_approval(session, org_b, admin_b, ticket.id, approve=True)


async def test_load_approval_org_id_predicate_is_exercised(
    api_env, admin_engine: AsyncEngine
) -> None:
    """B4 (whole-branch review, round B): `_load_approval`'s own
    `TicketApproval.org_id == org_id` predicate, isolated from `get_ticket`'s
    upstream org check -- `get_ticket(org_a, ticket.id)` succeeds since the
    ticket really does belong to org_a, so the upstream tenancy guard (the
    ONLY one `test_decide_approval_ticket_from_another_org_raises_not_found`
    above proves) is out of the picture entirely; only `_load_approval`'s own
    predicate stands between a foreign-stamped approval row and org_a's
    ability to decide it. Deciding an approval is the most privileged action
    in this module.

    `create_ticket` already wrote a genuine `pending` row for this ticket,
    stamped `org_id=org_a` -- foreign-stamp THAT row (a shape no correct
    caller ever produces) by updating its `org_id` to org_b directly, on a
    raw admin session, after the ticket has genuinely landed on
    `pending_approval` under org_a. With the predicate present, `decide_
    approval(org_a, ...)` can no longer find any row for this ticket and
    raises the "no ticket_approval row" invariant-violation error; with the
    predicate deleted, it finds the foreign-stamped row anyway (matching only
    on `ticket_id`), and the decision actually SUCCEEDS -- moving org_a's
    ticket to `in_progress` and stamping someone else's foreign-scoped row --
    which is exactly the privilege-escalation shape this predicate exists to
    prevent (mutation-verified, see `.git/sdd/fix-B-report.md`)."""
    org_a, admin_a = await _org_with_user(api_env, "sd-approvals-loadpred-a")
    org_b, _admin_b = await _org_with_user(api_env, "sd-approvals-loadpred-b")
    requester_id = await _second_user(api_env, org_a, "technician")
    approver_id = await _second_user(api_env, org_a, "org-admin")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket, _category = await _create_request(
            session, org_a, requester_id, admin_a, requires_approval=True
        )
        ticket_id = ticket.id
        approval = await _approval_row(session, org_a, ticket_id)
        assert approval is not None
        assert approval.state == "pending"

        # Foreign-stamp the row create_ticket itself just inserted -- no
        # correct caller ever produces this shape; it exists purely to
        # isolate _load_approval's own predicate.
        approval.org_id = org_b
        await session.flush()

        with pytest.raises(TicketValidationError) as exc:
            await decide_approval(session, org_a, approver_id, ticket_id, approve=True)
        assert "no ticket_approval row" in str(exc.value).lower()

        session.expire(ticket)
        reloaded_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        reloaded_approval = (
            await session.execute(
                select(TicketApproval).where(TicketApproval.ticket_id == ticket_id)
            )
        ).scalar_one()

    assert reloaded_ticket.status == "pending_approval"
    assert reloaded_approval.state == "pending"
    assert reloaded_approval.decided_by is None


# ---------------------------------------------------------------------------
# decide_approval -- audit and status history
# ---------------------------------------------------------------------------


async def test_approve_audits_exactly_once(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        before = await _history_rows(session, org_id, ticket.id)

        decided = await decide_approval(session, org_id, approver_id, ticket.id, approve=True)

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "ticket.approved"
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = await _history_rows(session, org_id, ticket.id)

    assert len(events) == 1
    event = events[0]
    assert event.target_type == "ticket"
    assert event.target_id == str(ticket.id)
    assert event.actor_id == approver_id
    assert event.meta == {"approval_id": str(decided.id)}
    # never the free-text reason (spec §8.3) -- there is none for an
    # approval, but the shape must not carry a "reason" key regardless.
    assert "reason" not in event.meta

    assert len(rows) == len(before) + 1
    newest = rows[-1]
    assert newest.from_status == "pending_approval"
    assert newest.to_status == "in_progress"


async def test_reject_audits_exactly_once_and_never_carries_the_reason_text(api_env) -> None:
    org_id, admin_id = await _org_with_user(api_env)
    requester_id = await _second_user(api_env, org_id, "technician")
    approver_id = await _second_user(api_env, org_id, "org-admin")

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket, _category = await _create_request(
            session, org_id, requester_id, admin_id, requires_approval=True
        )
        before = await _history_rows(session, org_id, ticket.id)

        decided = await decide_approval(
            session, org_id, approver_id, ticket.id, approve=False, reason="budget denied"
        )

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "ticket.rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = await _history_rows(session, org_id, ticket.id)

    assert len(events) == 1
    event = events[0]
    assert event.actor_id == approver_id
    assert event.meta == {"approval_id": str(decided.id)}
    assert "budget denied" not in str(event.meta)

    assert len(rows) == len(before) + 1
    newest = rows[-1]
    assert newest.from_status == "pending_approval"
    assert newest.to_status == "rejected"
