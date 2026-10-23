"""Ticket comments and attachments (docs/modules/service-desk/spec.md §2, §6,
§8.3).

Covers, in order: `add_comment` appends and `list_comments` returns rows
oldest-first; `author_type`/`visibility` are validated against the same
pairing `ck_comment_author` enforces at the database layer (a `system`
comment carries a NULL `author_id`, a `user` comment carries one, and an
unrecognized `author_type`/`visibility` is a typed 400 rather than an
`IntegrityError`); `include_internal=False` excludes `visibility="internal"`
comments (the portal sub-project's future need); a terminal ticket
(`lifecycle.TERMINAL_STATUSES`, never the narrower `FINAL_STATUSES`) accepts
no new comments -- pinned from BOTH directions, `cancelled` (in
`FINAL_STATUSES` too) and `closed` (deliberately reopenable, NOT in
`FINAL_STATUSES`, task 10's T1 fix) -- comments are append-only (no
edit/delete function exists to test); `add_comment` audits exactly once
(`ticket.comment_added`, never the body, and -- for a `system` comment,
task 10's T2 fix -- with `actor_type="system"`/`actor_id=None` on the AUDIT
EVENT itself, not just the comment row); a cross-org ticket raises
`TicketNotFoundError` from both functions; `list_comments`' own `org_id`
predicate is isolated from `get_ticket`'s upstream org check and proven to do
independent work (task 10's T3 fix).

Then attachments: the object key is entirely server-derived
(`{org_id}/service-desk/tickets/{ticket_id}/{attachment_id}`, no
client-supplied segment anywhere in it); `get_ticket_attachment` asserts the
key begins with `{org_id}/service-desk/`; oversized and invalid filenames are
rejected before the object store is ever touched; a terminal ticket accepts
no new attachments either (task 10's T4 decision -- an attachment is thread
content in the same sense a comment is); `add_attachment` audits exactly once
(`ticket.attachment_uploaded`); `get_attachment` is org-scoped and does not
audit (download is a route-level audited read, spec §6, not built in this
task); a cross-org ticket/attachment id raises `TicketNotFoundError`.

Finally, `sanitize_attachment_filename` is unit-tested DIRECTLY, not through
an API client -- the in-process ASGI harness this module has no route for
yet does not run h11 regardless, and even once a route exists that harness
cannot observe the CRLF-poisoning bug class this function exists to prevent
(see `tests/test_core_attachments.py`'s module docstring for the full
argument, and `app.core.attachments`'s module docstring for why the
sanitizer itself is now shared rather than copied).
"""

from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import org_session
from app.models import AuditEvent, Ticket, TicketAttachment, TicketComment, User
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.refdata import create_category, create_queue
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.threads import (
    add_attachment,
    add_comment,
    get_attachment,
    list_comments,
    sanitize_attachment_filename,
)
from app.modules.service_desk.tickets import create_ticket
from app.modules.service_desk.transitions import change_status

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_service_desk_approvals.py's shapes -- not
# imported across test modules, per convention)
# ---------------------------------------------------------------------------


async def _org_with_user(api_env, slug: str = "sd-threads") -> tuple[UUID, UUID]:
    org_id = await api_env.create_org(slug)
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


async def _create_incident(session, org_id, actor_id, *, requester_id=None) -> Ticket:
    queue = await create_queue(session, org_id, actor_id, key="helpdesk", name="Helpdesk")
    category = await create_category(
        session, org_id, actor_id, key="hardware", name="Hardware", type="incident"
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
            requester_id=requester_id or actor_id,
        ),
    )


@pytest.fixture()
def store(api_env):
    """A moto-backed `ObjectStore` built from the same `Settings` `api_env`
    already carries -- mirrors `tests/test_storage.py`'s `store` fixture."""
    with mock_aws():
        yield ObjectStore(api_env.settings)


# ---------------------------------------------------------------------------
# add_comment / list_comments
# ---------------------------------------------------------------------------


async def test_add_comment_appends_and_list_comments_returns_it_oldest_first(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        first = await add_comment(session, org_id, ticket.id, body="first", author_id=user_id)
        second = await add_comment(session, org_id, ticket.id, body="second", author_id=user_id)

        comments = await list_comments(session, org_id, ticket.id)

    assert [c.id for c in comments] == [first.id, second.id]
    assert comments[0].body == "first"
    assert comments[1].body == "second"


async def test_system_comment_requires_null_author_id(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        comment = await add_comment(
            session,
            org_id,
            ticket.id,
            body="auto-triaged",
            author_id=None,
            author_type="system",
        )
    assert comment.author_type == "system"
    assert comment.author_id is None


async def test_system_comment_audit_event_records_system_actor(api_env) -> None:
    """This is Task 8's S5 fix verbatim, third recurrence: asserts the
    `ticket.comment_added` AUDIT EVENT's own actor pairing, not just the
    `ticket_comment` row's `author_type`/`author_id`
    (`test_system_comment_requires_null_author_id` above only checks the
    latter). `AuditEvent` carries no actor-pairing constraint of its own, so
    hardcoding `actor_type="user"` in `add_comment`'s `audit_service.record`
    call passes every other test in this file -- the audit log would then
    attribute a Phase-15 auto-triage comment (`author_type="system"`,
    `author_id=None`) to an unattributed human, indistinguishable from a real
    person in the hash-chained log (invariant #4). Mutation-verify: hardcode
    `actor_type="user"` in that call, confirm this test fails, restore,
    confirm identical (see `.git/sdd/task-10-report.md`)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        comment = await add_comment(
            session,
            org_id,
            ticket.id,
            body="auto-triaged",
            author_id=None,
            author_type="system",
        )

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "ticket.comment_added"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert event.meta == {"comment_id": str(comment.id), "visibility": "public"}


async def test_system_comment_with_an_author_id_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        with pytest.raises(TicketValidationError):
            await add_comment(
                session,
                org_id,
                ticket.id,
                body="x",
                author_id=user_id,
                author_type="system",
            )


async def test_user_comment_with_no_author_id_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        with pytest.raises(TicketValidationError):
            await add_comment(session, org_id, ticket.id, body="x", author_id=None)


async def test_unknown_visibility_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        with pytest.raises(TicketValidationError):
            await add_comment(
                session, org_id, ticket.id, body="x", author_id=user_id, visibility="secret"
            )


async def test_add_comment_rejects_unknown_author_type(api_env) -> None:
    """B4 (whole-branch review, round B): `_VALID_AUTHOR_TYPES`
    (`{"user", "system"}`) was entirely unexercised. `author_type="bot"` with
    `author_id=None` passes BOTH pairing checks below it -- it is not
    `"user"` (so the "author_id required" branch never fires) and not
    `"system"` (so the "author_id must be None" branch never fires either) --
    and would otherwise reach the column unrejected, where `ck_comment_author`
    (`(author_type='user' AND author_id NOT NULL) OR (author_type='system'
    AND author_id IS NULL)`) is satisfied by NEITHER clause for `'bot'`,
    surfacing as an `IntegrityError` (a 500) rather than this function's own
    typed 400. `change_status`'s equivalent
    (`test_unknown_actor_type_is_rejected` in
    `test_service_desk_transitions.py`) IS tested; this was the skipped half
    of that pair."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        with pytest.raises(TicketValidationError) as exc:
            await add_comment(
                session, org_id, ticket.id, body="x", author_id=None, author_type="bot"
            )
        assert "unknown author_type" in str(exc.value).lower()

        remaining = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == ticket.id)
                )
            )
            .scalars()
            .all()
        )
    assert remaining == []


async def test_include_internal_false_excludes_internal_comments(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        await add_comment(
            session, org_id, ticket.id, body="visible to the requester", author_id=user_id
        )
        await add_comment(
            session,
            org_id,
            ticket.id,
            body="internal triage note",
            author_id=user_id,
            visibility="internal",
        )

        everything = await list_comments(session, org_id, ticket.id, include_internal=True)
        public_only = await list_comments(session, org_id, ticket.id, include_internal=False)

    assert len(everything) == 2
    assert len(public_only) == 1
    assert public_only[0].visibility == "public"


async def test_adding_a_comment_to_a_terminal_ticket_is_rejected(api_env) -> None:
    """Spec §6/§3.3: `TERMINAL_STATUSES` (`closed`, `cancelled`, `rejected`)
    gates new comments. `new -> cancelled` is a legal incident transition
    (`lifecycle.py`), reached directly with no detour needed."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await change_status(session, org_id, ticket.id, to_status="cancelled", actor_id=user_id)

        with pytest.raises(TicketValidationError):
            await add_comment(session, org_id, ticket.id, body="too late", author_id=user_id)


async def test_adding_a_comment_to_a_closed_ticket_is_rejected(api_env) -> None:
    """`closed` is in `TERMINAL_STATUSES` (comments gated) but NOT in
    `FINAL_STATUSES` (nothing ever leaves) -- spec §3.3 distinguishes the two
    on purpose, since `closed` is deliberately reopenable via
    `transitions.change_status` even though it accepts no new comments in the
    meantime. `test_adding_a_comment_to_a_terminal_ticket_is_rejected` above
    forces `cancelled`, which is in BOTH sets, so it alone cannot catch
    `_reject_if_terminal_for_thread_content` being gated on `FINAL_STATUSES`
    instead of `TERMINAL_STATUSES` -- that swap would leave every `closed`
    ticket silently accepting comments forever while this suite stayed green
    (rewriting the import as `FINAL_STATUSES as TERMINAL_STATUSES` gets 22
    passed with only the cancelled-ticket test in place). This is Task 7's
    R3 fix, one rule over: `tests.test_service_desk_tickets.
    test_closed_ticket_rejects_edits` is the template. This case, forcing
    `closed` specifically via a real `open -> resolved -> closed` transition
    sequence (not a raw `ticket.status =` assignment), is the one that would
    catch it (mutation-verified: see `.git/sdd/task-10-report.md`)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await change_status(session, org_id, ticket.id, to_status="open", actor_id=user_id)
        await change_status(session, org_id, ticket.id, to_status="resolved", actor_id=user_id)
        await change_status(session, org_id, ticket.id, to_status="closed", actor_id=user_id)

        with pytest.raises(TicketValidationError):
            await add_comment(session, org_id, ticket.id, body="too late", author_id=user_id)


async def test_add_comment_audits_exactly_once_and_never_carries_the_body(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        comment = await add_comment(
            session, org_id, ticket.id, body="secret diagnostic detail", author_id=user_id
        )

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id, AuditEvent.action == "ticket.comment_added"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    event = events[0]
    assert event.target_type == "ticket"
    assert event.target_id == str(ticket.id)
    assert event.meta == {"comment_id": str(comment.id), "visibility": "public"}
    assert "secret diagnostic detail" not in str(event.meta)


async def test_add_comment_cross_org_ticket_raises_not_found(
    api_env, admin_engine: AsyncEngine
) -> None:
    org_a, admin_a = await _org_with_user(api_env, "sd-threads-a")
    org_b, admin_b = await _org_with_user(api_env, "sd-threads-b")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, admin_a)

        with pytest.raises(TicketNotFoundError):
            await add_comment(session, org_b, ticket.id, body="x", author_id=admin_b)

        with pytest.raises(TicketNotFoundError):
            await list_comments(session, org_b, ticket.id)


async def test_list_comments_org_id_predicate_is_exercised(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Spec §8.1/invariant #5: `list_comments`' own query carries an explicit
    `TicketComment.org_id == org_id` predicate IN ADDITION to `get_ticket`'s
    upstream org-scoped load. `test_add_comment_cross_org_ticket_raises_not_
    found` above proves nothing about this predicate specifically: passing a
    ticket that belongs to a different org is caught by `get_ticket` alone,
    before the comments query even runs, so deleting `TicketComment.org_id ==
    org_id` from `threads.list_comments` still leaves every existing test in
    this file green (Task 9's lesson: when two guards can reject the same
    call, a test proves nothing about which one fired).

    So this constructs the one state only this predicate can refuse: on a
    raw admin session (bypassing RLS and `add_comment`'s own org-scoped
    insert), attach a comment row to org_a's ticket but stamp it with org_b's
    `org_id` -- a shape no correct caller can ever produce through this
    module's own functions, but the query itself does not know that.
    `get_ticket(org_a, ticket.id)` succeeds, since the ticket itself really
    does belong to org_a, so the upstream guard is out of the picture
    entirely; only `list_comments`' own `org_id` predicate stands between
    this foreign-stamped row and org_a's listing (mutation-verified: see
    `.git/sdd/task-10-report.md`)."""
    org_a, admin_a = await _org_with_user(api_env, "sd-threads-list-a")
    org_b, admin_b = await _org_with_user(api_env, "sd-threads-list-b")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, admin_a)
        own_comment = await add_comment(session, org_a, ticket.id, body="mine", author_id=admin_a)

        # A shape `add_comment` itself never produces -- a comment row whose
        # `ticket_id` points at org_a's ticket but whose `org_id` is org_b's.
        # No FK/CHECK ties the two together, so this insert succeeds; it
        # exists purely to isolate the query's own predicate.
        foreign = TicketComment(
            org_id=org_b,
            ticket_id=ticket.id,
            author_id=admin_b,
            author_type="user",
            body="must never surface under org_a's listing",
            visibility="public",
        )
        session.add(foreign)
        await session.flush()

        comments = await list_comments(session, org_a, ticket.id)

    assert [c.id for c in comments] == [own_comment.id]


# ---------------------------------------------------------------------------
# add_attachment / get_attachment
# ---------------------------------------------------------------------------


async def test_add_attachment_key_is_entirely_server_derived(api_env, store: ObjectStore) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        attachment = await add_attachment(
            session,
            org_id,
            user_id,
            ticket.id,
            filename="screenshot.png",
            data=b"fake-png-bytes",
            store=store,
        )

    expected_key = f"{org_id}/service-desk/tickets/{ticket.id}/{attachment.id}"
    assert attachment.object_key.endswith(expected_key)
    # No client-supplied segment anywhere in the key -- not the filename, not
    # its extension.
    assert "screenshot" not in attachment.object_key
    assert attachment.filename == "screenshot.png"
    assert attachment.byte_size == len(b"fake-png-bytes")


async def test_add_attachment_and_get_attachment_roundtrip_the_bytes(
    api_env, store: ObjectStore
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        attachment = await add_attachment(
            session,
            org_id,
            user_id,
            ticket.id,
            filename="log.txt",
            data=b"log contents here",
            store=store,
        )

        loaded = await get_attachment(session, org_id, attachment.id)

    assert loaded.id == attachment.id
    assert loaded.filename == "log.txt"
    data = await store.get_ticket_attachment(loaded.object_key, org_id)
    assert data == b"log contents here"


async def test_get_ticket_attachment_asserts_org_prefix(api_env, store: ObjectStore) -> None:
    """Defence in depth (context note #2): even though the key is always
    server-derived, `get_ticket_attachment` refuses to serve it back to a
    caller passing the wrong `org_id`."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        attachment = await add_attachment(
            session,
            org_id,
            user_id,
            ticket.id,
            filename="a.pdf",
            data=b"bytes",
            store=store,
        )

    with pytest.raises(ValueError):
        await store.get_ticket_attachment(attachment.object_key, uuid4())


async def test_add_attachment_rejects_invalid_filenames_before_touching_the_store(
    api_env, store: ObjectStore
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        for bad in ("a.pdf\n", "../x.pdf", "発票.pdf", ""):
            with pytest.raises(TicketValidationError):
                await add_attachment(
                    session,
                    org_id,
                    user_id,
                    ticket.id,
                    filename=bad,
                    data=b"bytes",
                    store=store,
                )

        # None of the rejected uploads left an attachment row behind.
        remaining = (
            (
                await session.execute(
                    select(TicketAttachment).where(TicketAttachment.ticket_id == ticket.id)
                )
            )
            .scalars()
            .all()
        )
    assert remaining == []


async def test_add_attachment_rejects_oversized_body(api_env, store: ObjectStore) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        oversized = b"x" * (10 * 1024 * 1024 + 1)
        with pytest.raises(TicketValidationError):
            await add_attachment(
                session,
                org_id,
                user_id,
                ticket.id,
                filename="big.bin",
                data=oversized,
                store=store,
            )


async def test_add_attachment_to_a_terminal_ticket_is_rejected(
    api_env, store: ObjectStore
) -> None:
    """DECIDED (task 10's fix round, T4): `add_attachment` is gated on
    `lifecycle.TERMINAL_STATUSES`, identically to `add_comment` -- an
    attachment is thread content in the same sense a comment is (spec §6), so
    a ticket that accepts no new comments accepts no new files either.
    `new -> cancelled` is a legal incident transition, reached directly with
    no detour needed. No attachment row is left behind by the rejected
    upload."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        await change_status(session, org_id, ticket.id, to_status="cancelled", actor_id=user_id)

        with pytest.raises(TicketValidationError):
            await add_attachment(
                session,
                org_id,
                user_id,
                ticket.id,
                filename="too-late.pdf",
                data=b"bytes",
                store=store,
            )

        remaining = (
            (
                await session.execute(
                    select(TicketAttachment).where(TicketAttachment.ticket_id == ticket.id)
                )
            )
            .scalars()
            .all()
        )
    assert remaining == []


async def test_add_attachment_audits_exactly_once(api_env, store: ObjectStore) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)

        attachment = await add_attachment(
            session,
            org_id,
            user_id,
            ticket.id,
            filename="invoice.pdf",
            data=b"bytes",
            store=store,
        )

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "ticket.attachment_uploaded",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(events) == 1
    event = events[0]
    assert event.target_type == "ticket"
    assert event.target_id == str(ticket.id)
    # B2 (whole-branch review, round B): attribution was never asserted here.
    assert event.actor_type == "user"
    assert event.actor_id == user_id
    assert event.meta == {"attachment_id": str(attachment.id), "filename": "invoice.pdf"}


async def test_add_attachment_cross_org_ticket_raises_not_found(
    api_env, admin_engine: AsyncEngine, store: ObjectStore
) -> None:
    org_a, admin_a = await _org_with_user(api_env, "sd-threads-attach-a")
    org_b, admin_b = await _org_with_user(api_env, "sd-threads-attach-b")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, admin_a)

        with pytest.raises(TicketNotFoundError):
            await add_attachment(
                session,
                org_b,
                admin_b,
                ticket.id,
                filename="x.pdf",
                data=b"bytes",
                store=store,
            )


async def test_get_attachment_cross_org_raises_not_found(
    api_env, admin_engine: AsyncEngine, store: ObjectStore
) -> None:
    org_a, admin_a = await _org_with_user(api_env, "sd-threads-attach-c")
    org_b, _admin_b = await _org_with_user(api_env, "sd-threads-attach-d")

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        ticket = await _create_incident(session, org_a, admin_a)
        attachment = await add_attachment(
            session,
            org_a,
            admin_a,
            ticket.id,
            filename="x.pdf",
            data=b"bytes",
            store=store,
        )

        with pytest.raises(TicketNotFoundError):
            await get_attachment(session, org_b, attachment.id)


async def test_get_attachment_does_not_audit(api_env, store: ObjectStore) -> None:
    """Download IS the audited read (spec §6, §8.3) -- but that is a
    route-level concern with no route yet in this task. `get_attachment`
    itself, the plain org-scoped load, writes nothing (the no-audit-on-view
    rule every other module in this platform already follows)."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        ticket = await _create_incident(session, org_id, user_id)
        attachment = await add_attachment(
            session,
            org_id,
            user_id,
            ticket.id,
            filename="x.pdf",
            data=b"bytes",
            store=store,
        )

        before = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        await get_attachment(session, org_id, attachment.id)
        after = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(after) == len(before)


# ---------------------------------------------------------------------------
# `sanitize_attachment_filename` unit tests.
#
# Driven DIRECTLY rather than through an API client, deliberately -- this
# module has no route yet, and even once one exists the in-process ASGI test
# harness does not run h11, so a filename that would kill every download
# under uvicorn still returns fine here. See `app.core.attachments`'s module
# docstring for the two load-bearing subtleties (`re.ASCII`, `fullmatch`)
# and `tests/test_core_attachments.py` for the moved-unchanged Budget tests
# proving the shared extraction did not change behavior there either.
# ---------------------------------------------------------------------------


def test_sanitize_rejects_a_trailing_newline() -> None:
    """Python's `$` matches immediately BEFORE a trailing newline, so a
    `^...$` allow-list accepts "a.pdf\\n" -- which then reaches
    Content-Disposition and kills every download with h11's
    `LocalProtocolError`. `re.fullmatch` is what closes it.

    This is unit-tested rather than driven through the API because the
    in-process ASGI harness does not run h11: an HTTP-level test returns 200
    here and the bug only appears under uvicorn.
    """
    for bad in ("a.pdf\n", "a.pdf\r", "a.pdf\r\n"):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitize_rejects_non_ascii() -> None:
    """Bare `\\w` is Unicode-wide; Starlette encodes headers as latin-1, so an
    ordinary non-Latin filename uploads fine and then makes every download a
    500 for the whole org. `re.ASCII` gives the user a fixable 400 instead."""
    for bad in ("発票.pdf", "счёт.pdf"):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitize_rejects_paths_traversal_and_empty() -> None:
    for bad in ("", "..", "../x.pdf", "a/b.pdf", "C:\\evil\\x.pdf", "x" * 256):
        with pytest.raises(TicketValidationError):
            sanitize_attachment_filename(bad)


def test_sanitized_value_is_header_safe() -> None:
    for good in ("invoice.pdf", "INV-2026 03.pdf", "a_b-c.1.pdf"):
        header = f'attachment; filename="{sanitize_attachment_filename(good)}"'
        header.encode("latin-1")
