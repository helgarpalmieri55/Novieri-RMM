"""Portal services (spec §5, §6, §2.3).

The 404-not-403 test and the internal-note test are the two that matter: one
prevents a cross-requester existence oracle, the other prevents disclosing
staff discussion about a person to that person.
"""

from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import ObjectStore
from app.core.tenancy import portal_session
from app.models import Ticket, User
from app.modules.portal.errors import PortalNotFoundError
from app.modules.portal.schema import PortalCommentCreate, PortalTicketCreate, PortalTicketDetail
from app.modules.portal.tickets import (
    add_my_attachment,
    add_my_comment,
    create_my_ticket,
    get_my_ticket,
    list_categories,
    list_my_tickets,
)
from app.modules.service_desk import threads as sd_threads
from app.modules.service_desk.errors import TicketNotFoundError
from app.modules.service_desk.refdata import create_category, create_queue


@pytest.fixture()
def store(api_env):
    """A moto-backed `ObjectStore` -- mirrors `test_service_desk_threads.py`'s
    `store` fixture, needed here for `add_my_attachment`'s native-write test."""
    from moto import mock_aws

    with mock_aws():
        yield ObjectStore(api_env.settings)


def test_portal_detail_exposes_no_staff_fields() -> None:
    """Spec §5 -- a structural guarantee, not a runtime filter. The portal must
    not reuse the staff TicketOut."""
    fields = set(PortalTicketDetail.model_fields)
    for forbidden in ("queue_id", "assignee_id", "assigned_to", "internal_notes"):
        assert forbidden not in fields, f"portal detail exposes {forbidden}"


def test_status_history_out_has_no_reason_field() -> None:
    """C1 -- structural guarantee, the same shape as the test above.
    `ticket_status_history.reason` is unbounded staff free text with no
    visibility column (spec §5's amendment); `PortalStatusHistoryOut` must
    never carry it at all, so a future careless `reason=history.reason`
    cannot silently reintroduce the leak this field name once caused."""
    from app.modules.portal.schema import PortalStatusHistoryOut

    assert "reason" not in PortalStatusHistoryOut.model_fields


async def test_a_staff_reason_never_reaches_the_portal_detail_response(
    api_env, portal_engine, a_ticket_for
) -> None:
    """C1 -- the exact trigger from the whole-branch review: a technician
    changes a ticket's status with an internal-facing reason
    ("INTERNAL: repeat offender, see HR case 42, do not escalate"); the
    requester's own GET must not surface that string anywhere in the
    response, and the field itself must be structurally absent (see the test
    above), not merely empty at runtime."""
    from app.modules.service_desk.transitions import change_status

    org_id = await api_env.create_org("portal-c1")
    mine = await a_ticket_for(org_id, "me")
    staff_reason = "INTERNAL: repeat offender, see HR case 42, do not escalate"

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            staff = User(org_id=org_id, external_subject="staff-c1", email="staff-c1@test.local")
            session.add(staff)
            await session.flush()
            await change_status(
                session,
                org_id,
                mine.id,
                to_status="open",
                actor_id=staff.id,
                reason=staff_reason,
            )

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        detail = await get_my_ticket(
            s, org_id=org_id, requester_id=mine.requester_id, ticket_id=mine.id
        )

    # `a_ticket_for` inserts the `Ticket` row directly (not via
    # `create_ticket`), so there is no separate "creation" history row here --
    # just the one `change_status` call above.
    assert len(detail.status_history) == 1
    for entry in detail.status_history:
        assert not hasattr(entry, "reason")
    dumped = detail.model_dump_json()
    assert staff_reason not in dumped
    assert "HR case 42" not in dumped
    assert "repeat offender" not in dumped


async def test_a_ticket_that_is_not_mine_is_not_found(api_env, portal_engine, a_ticket_for) -> None:
    """Spec §5 -- 404, never 403. A 403 confirms the id exists, which is a
    cross-requester existence oracle."""
    org_id = await api_env.create_org("portal-404")
    theirs = await a_ticket_for(org_id, "them")
    me = await a_ticket_for(org_id, "me")

    async with portal_session(org_id, me.requester_id, engine=portal_engine) as s:
        with pytest.raises(PortalNotFoundError):
            await get_my_ticket(s, org_id=org_id, requester_id=me.requester_id, ticket_id=theirs.id)


async def test_an_absent_ticket_raises_the_identical_error_as_not_mine(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Absent and not-mine must be INDISTINGUISHABLE -- same exception type,
    same message shape, no separate code path (spec §5)."""
    from uuid import uuid4

    org_id = await api_env.create_org("portal-404-absent")
    theirs = await a_ticket_for(org_id, "them")
    me = await a_ticket_for(org_id, "me")
    nonexistent_id = uuid4()

    async with portal_session(org_id, me.requester_id, engine=portal_engine) as s:
        with pytest.raises(PortalNotFoundError) as not_mine:
            await get_my_ticket(s, org_id=org_id, requester_id=me.requester_id, ticket_id=theirs.id)
        with pytest.raises(PortalNotFoundError) as absent:
            await get_my_ticket(
                s, org_id=org_id, requester_id=me.requester_id, ticket_id=nonexistent_id
            )

    assert type(not_mine.value) is type(absent.value)
    # Same message SHAPE (identifier substituted, no other distinguishing text) --
    # neither leaks whether the id belongs to someone else vs. not existing at all.
    assert str(not_mine.value) == f"ticket {theirs.id} not found"
    assert str(absent.value) == f"ticket {nonexistent_id} not found"


async def test_internal_notes_are_absent_from_the_thread(
    api_env, portal_engine, a_ticket_for, add_comment_as_staff
) -> None:
    """Spec §9.3 -- MUST fail if the POLICY's visibility filter is removed, not
    only if the query filter is. Assert on the returned thread."""
    org_id = await api_env.create_org("portal-internal")
    mine = await a_ticket_for(org_id, "me")
    await add_comment_as_staff(mine, body="visible to you", internal=False)
    await add_comment_as_staff(mine, body="STAFF ONLY do not disclose", internal=True)

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        detail = await get_my_ticket(
            s, org_id=org_id, requester_id=mine.requester_id, ticket_id=mine.id
        )
    bodies = [c.body for c in detail.comments]
    assert "visible to you" in bodies
    assert all("STAFF ONLY" not in b for b in bodies)


async def test_list_my_tickets_excludes_another_requesters_ticket(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Cross-requester isolation on the LIST path, query-level belt in
    addition to RLS's brace (spec §9.2)."""
    org_id = await api_env.create_org("portal-list")
    mine = await a_ticket_for(org_id, "me")
    theirs = await a_ticket_for(org_id, "them")

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        summaries = await list_my_tickets(s, org_id=org_id, requester_id=mine.requester_id)

    ids = {t.id for t in summaries}
    assert mine.id in ids
    assert theirs.id not in ids


async def _org_with_staff_actor(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus a user id to attribute admin-session
    refdata creation to (mirrors `test_service_desk_tickets.py`'s
    `_org_with_user` helper -- not imported across test modules, per
    convention)."""
    org_id = await api_env.create_org("portal-tickets")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        actor_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, actor_id


async def test_creating_a_ticket_forces_me_as_requester(api_env, portal_engine) -> None:
    """Spec §6.2 -- requester_id comes from PortalContext, never the body."""
    org_id = await api_env.create_org("portal-create")
    sub = await api_env.create_user(org_id, "requester")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        requester_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()

    # Category + its default queue, created via the admin (RLS-bypassing)
    # engine -- this is org refdata a staff member manages, not something the
    # portal test itself needs to go through portal_session to set up.
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            queue = await create_queue(
                session, org_id, requester_id, key="helpdesk", name="Helpdesk"
            )
            category = await create_category(
                session,
                org_id,
                requester_id,
                key="hardware",
                name="Hardware",
                type="incident",
                default_queue_id=queue.id,
            )
        category_id = category.id

    async with portal_session(org_id, requester_id, engine=portal_engine) as s:
        detail = await create_my_ticket(
            s,
            org_id=org_id,
            requester_id=requester_id,
            data=PortalTicketCreate(category_id=category_id, subject="my laptop is broken"),
        )

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = (await session.execute(select(Ticket).where(Ticket.id == detail.id))).scalar_one()
    assert row.requester_id == requester_id
    assert row.org_id == org_id
    assert row.origin == "portal"


async def test_add_my_comment_appears_in_the_thread_as_public(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Spec §6.2 -- add_my_comment forces visibility to public regardless of
    anything the caller might otherwise have supplied."""
    org_id = await api_env.create_org("portal-comment")
    mine = await a_ticket_for(org_id, "me")

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        out = await add_my_comment(
            s,
            org_id=org_id,
            requester_id=mine.requester_id,
            ticket_id=mine.id,
            data=PortalCommentCreate(body="any update?"),
        )
        assert out.body == "any update?"

        detail = await get_my_ticket(
            s, org_id=org_id, requester_id=mine.requester_id, ticket_id=mine.id
        )
    assert "any update?" in [c.body for c in detail.comments]


async def test_add_my_comment_on_someone_elses_ticket_is_not_found(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Cross-requester isolation on the WRITE path (spec §9.2, §9.6): a
    requester cannot comment on a ticket that is not theirs, and the failure
    is the same 404 as an absent ticket."""
    org_id = await api_env.create_org("portal-comment-404")
    theirs = await a_ticket_for(org_id, "them")
    me = await a_ticket_for(org_id, "me")

    async with portal_session(org_id, me.requester_id, engine=portal_engine) as s:
        with pytest.raises(PortalNotFoundError):
            await add_my_comment(
                s,
                org_id=org_id,
                requester_id=me.requester_id,
                ticket_id=theirs.id,
                data=PortalCommentCreate(body="sneaking in"),
            )


async def test_list_categories_returns_only_active_org_categories(api_env) -> None:
    org_id, actor_id = await _org_with_staff_actor(api_env)
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            queue = await create_queue(session, org_id, actor_id, key="helpdesk", name="Helpdesk")
            await create_category(
                session,
                org_id,
                actor_id,
                key="hardware",
                name="Hardware",
                type="incident",
                default_queue_id=queue.id,
            )
            inactive = await create_category(
                session,
                org_id,
                actor_id,
                key="retired",
                name="Retired",
                type="incident",
                default_queue_id=queue.id,
            )
            inactive.active = False

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        cats = await list_categories(session, org_id=org_id)
    keys = {c.key for c in cats}
    assert "hardware" in keys
    assert "retired" not in keys


# ---------------------------------------------------------------------------
# I1/I2 -- ticket_attachment has no visibility column, so PortalTicketDetail.
# attachments (and the download route, tested in test_portal_api.py) must
# scope reads to uploaded_by = the requester, not just parent-ticket-is-mine.
# ---------------------------------------------------------------------------


async def test_get_my_ticket_lists_only_the_requesters_own_attachments(
    api_env, portal_engine, a_ticket_for, store
) -> None:
    """A staff-uploaded attachment on the requester's OWN ticket must never
    appear in `PortalTicketDetail.attachments` -- the same scoping the
    download route applies (spec §2.1's amendment, finding I1)."""
    org_id = await api_env.create_org("portal-attachments-scope")
    mine = await a_ticket_for(org_id, "me")

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            staff = User(org_id=org_id, external_subject="staff-i1", email="staff-i1@test.local")
            session.add(staff)
            await session.flush()
            await sd_threads.add_attachment(
                session,
                org_id,
                staff.id,
                mine.id,
                filename="dossier.txt",
                data=b"STAFF ONLY escalation dossier",
                store=store,
            )

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        my_attachment = await add_my_attachment(
            s,
            org_id=org_id,
            requester_id=mine.requester_id,
            ticket_id=mine.id,
            filename="screenshot.png",
            data=b"my own upload",
            store=store,
        )
        detail = await get_my_ticket(
            s, org_id=org_id, requester_id=mine.requester_id, ticket_id=mine.id
        )

    ids = {a.id for a in detail.attachments}
    assert ids == {my_attachment.id}, "a staff-uploaded attachment leaked into the list"


# ---------------------------------------------------------------------------
# Migration 0030 -- portal writes run natively under portal_session, with no
# second, elevated connection; the database (not application code) is what
# refuses a cross-requester write.
# ---------------------------------------------------------------------------


async def test_add_my_attachment_succeeds_natively_under_portal_session(
    api_env, portal_engine, a_ticket_for, store
) -> None:
    """The third write path. No `engine=` kwarg exists any more -- this can
    only run on the single `portal_session` connection `s` provides."""
    org_id = await api_env.create_org("portal-attachment")
    mine = await a_ticket_for(org_id, "me")

    async with portal_session(org_id, mine.requester_id, engine=portal_engine) as s:
        out = await add_my_attachment(
            s,
            org_id=org_id,
            requester_id=mine.requester_id,
            ticket_id=mine.id,
            filename="screenshot.png",
            data=b"not-really-a-png",
            store=store,
        )
    assert out.filename == "screenshot.png"
    assert out.byte_size == len(b"not-really-a-png")


async def test_the_three_write_paths_all_run_on_one_portal_session_connection(
    api_env, portal_engine, store
) -> None:
    """Required proof: create, comment, and attach all succeed using ONLY the
    caller's own `portal_session`-opened `s` -- reused across all three calls,
    never a second engine or a second session anywhere in this test."""
    org_id = await api_env.create_org("portal-native")
    sub = await api_env.create_user(org_id, "requester")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        requester_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        async with session.begin():
            queue = await create_queue(
                session, org_id, requester_id, key="helpdesk", name="Helpdesk"
            )
            category = await create_category(
                session,
                org_id,
                requester_id,
                key="hardware",
                name="Hardware",
                type="incident",
                default_queue_id=queue.id,
            )
        category_id = category.id

    async with portal_session(org_id, requester_id, engine=portal_engine) as s:
        detail = await create_my_ticket(
            s,
            org_id=org_id,
            requester_id=requester_id,
            data=PortalTicketCreate(category_id=category_id, subject="one connection only"),
        )
        comment = await add_my_comment(
            s,
            org_id=org_id,
            requester_id=requester_id,
            ticket_id=detail.id,
            data=PortalCommentCreate(body="still the same connection"),
        )
        attachment = await add_my_attachment(
            s,
            org_id=org_id,
            requester_id=requester_id,
            ticket_id=detail.id,
            filename="log.txt",
            data=b"still the same connection too",
            store=store,
        )

    assert detail.subject == "one connection only"
    assert comment.body == "still the same connection"
    assert attachment.filename == "log.txt"


async def test_a_requester_sees_only_their_own_user_row(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Migration 0030's `users` policy. A plain SELECT grant with no
    restrictive policy would let a requester read every colleague's name and
    email in the org via the pre-existing org-scoped tenant_isolation policy
    alone -- asserted here with a SECOND real user present in the same org."""
    org_id = await api_env.create_org("portal-users")
    me = await a_ticket_for(org_id, "me")
    them = await a_ticket_for(org_id, "them")

    async with portal_session(org_id, me.requester_id, engine=portal_engine) as s:
        rows = (await s.execute(select(User))).scalars().all()

    visible_ids = {row.id for row in rows}
    assert visible_ids == {me.requester_id}
    assert them.requester_id not in visible_ids


async def test_cross_requester_write_is_refused_by_the_database(
    api_env, portal_engine, a_ticket_for
) -> None:
    """THE point of this migration. Bypass the service-level ownership check
    entirely -- call `service_desk.threads.add_comment` DIRECTLY, never
    `portal.tickets.add_my_comment` (so `_load_my_ticket`'s 404 never runs) --
    under a `portal_session` scoped to a DIFFERENT requester than the
    ticket's owner. If portal writes were only application-checked, this
    would either write the comment or crash on something unrelated; instead
    the ticket becomes invisible under RLS the moment the wrong requester GUC
    is set, and `add_comment`'s own `get_ticket` lookup raises
    `TicketNotFoundError` -- the DATABASE refusing the write, not this
    module's ownership check (which was never called)."""
    org_id = await api_env.create_org("portal-db-enforced")
    mine = await a_ticket_for(org_id, "me")
    them = await a_ticket_for(org_id, "them")

    async with portal_session(org_id, them.requester_id, engine=portal_engine) as s:
        with pytest.raises(TicketNotFoundError):
            await sd_threads.add_comment(
                s,
                org_id,
                mine.id,
                body="I should never be able to write this",
                author_id=them.requester_id,
                author_type="user",
                visibility="public",
            )


async def test_cross_requester_status_history_insert_is_refused_by_the_database(
    api_env, portal_engine, a_ticket_for
) -> None:
    """Same point, at the raw-row level: a direct INSERT into
    `ticket_status_history` for a ticket belonging to a DIFFERENT requester,
    issued straight through the ORM under `portal_session` (no service-layer
    function involved at all), is refused by the existing `portal_isolation`
    policy's `USING` clause -- confirming empirically that a policy with no
    `WITH CHECK` still constrains INSERT (migration 0030's docstring)."""
    from sqlalchemy.exc import ProgrammingError

    from app.models import TicketStatusHistory

    org_id = await api_env.create_org("portal-db-enforced-history")
    mine = await a_ticket_for(org_id, "me")
    them = await a_ticket_for(org_id, "them")

    with pytest.raises(ProgrammingError, match="row-level security policy"):
        async with portal_session(org_id, them.requester_id, engine=portal_engine) as s:
            s.add(
                TicketStatusHistory(
                    org_id=org_id,
                    ticket_id=mine.id,
                    from_status=None,
                    to_status="new",
                    actor_type="user",
                    actor_id=them.requester_id,
                    reason="",
                )
            )
            await s.flush()
