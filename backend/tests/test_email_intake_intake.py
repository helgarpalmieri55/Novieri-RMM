"""The disposition engine: spec §8's table, executed, plus §4's idempotency.

Every assertion here is on STORED STATE -- an `inbound_message` row, a
`ticket` row, a `ticket_comment` row -- never on whether a function was
called. A test that asserts on the call passes against an implementation
that calls the right thing and stores the wrong thing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from moto import mock_aws
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.storage import ObjectStore
from app.core.tenancy import admin_session, org_session
from app.models import EmailMailbox
from app.modules.email_intake import intake as intake_module
from app.modules.email_intake.identity import mint_email_token
from app.modules.email_intake.intake import AlreadyProcessed, process_message, reopen_target
from app.modules.email_intake.models import InboundMessage
from app.modules.email_intake.parsing import ParsedAttachment, ParsedMessage
from app.modules.service_desk.models import Ticket, TicketComment
from app.modules.service_desk.schema import TicketCreate
from app.modules.service_desk.tickets import create_ticket
from app.modules.service_desk.transitions import change_status
from tests.test_email_intake_migration import _seed_category, _seed_credential, _seed_user

# `_parsed(...)` builds a ParsedMessage directly rather than round-tripping
# through raw bytes: Task 3 already tests parsing, and constructing the
# dataclass keeps these tests about DISPOSITION.

# The status-transition path `a_ticket_for` drives a freshly created ticket
# down to reach a requested end state. Every hop is an edge `lifecycle.py`
# actually defines; `create_ticket` lands an incident on `new` and an
# approval-free request on `in_progress`.
_PATH_TO_STATUS: dict[tuple[str, str], tuple[str, ...]] = {
    ("incident", "resolved"): ("open", "resolved"),
    ("incident", "closed"): ("open", "resolved", "closed"),
    ("incident", "cancelled"): ("cancelled",),
    ("request", "fulfilled"): ("fulfilled",),
    ("request", "closed"): ("fulfilled", "closed"),
}


async def _seed_request_category(api_env: Any, org_id: UUID) -> UUID:
    """`_seed_category` only ever makes an `incident` category; the request
    reopen edges (`fulfilled -> in_progress`, `closed -> in_progress`) need a
    `request`-typed one. Same throwaway-owner trick, same `@seed.invalid`
    domain, so the owner can never collide with a `From:` address these tests
    match on."""
    from app.models import User
    from app.modules.service_desk.models import TicketCategory, TicketQueue

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        creator = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"seed-request-owner-{uuid4().hex}@seed.invalid",
        )
        session.add(creator)
        await session.flush()
        queue = TicketQueue(
            org_id=org_id,
            key=f"rq-{uuid4().hex[:6]}",
            name=f"rq-{uuid4().hex[:6]}",
            created_by=creator.id,
        )
        session.add(queue)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"rc-{uuid4().hex[:6]}",
            name=f"rc-{uuid4().hex[:6]}",
            type="request",
            requires_approval=False,
            default_queue_id=queue.id,
            created_by=creator.id,
        )
        session.add(category)
        await session.commit()
        return category.id


async def _seed_queueless_category(api_env: Any, org_id: UUID) -> UUID:
    """A category with NO `default_queue_id`. `create_ticket` refuses it, and
    F3 is about what happens next.

    Task 9 closed the front door: `mailboxes._resolve_category` now REFUSES a
    default category with no queue, at mailbox create and update, so an
    org-admin can no longer configure this through the API. The tests below
    still matter and still bypass that guard deliberately (they assign
    `default_category_id` on the row directly) -- a category can lose its
    queue AFTER the mailbox was configured, and `process_message` must
    record a row rather than stall the mailbox whenever `create_ticket`
    refuses, for that reason and any other."""
    from app.models import User
    from app.modules.service_desk.models import TicketCategory

    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        creator = User(
            org_id=org_id,
            external_subject=f"sub-{uuid4().hex[:10]}",
            email=f"seed-queueless-owner-{uuid4().hex}@seed.invalid",
        )
        session.add(creator)
        await session.flush()
        category = TicketCategory(
            org_id=org_id,
            key=f"nq-{uuid4().hex[:6]}",
            name=f"nq-{uuid4().hex[:6]}",
            type="incident",
            requires_approval=False,
            default_queue_id=None,
            created_by=creator.id,
        )
        session.add(category)
        await session.commit()
        return category.id


@dataclass
class _OtherOrg:
    """A second org, wholly independent of `EmailEnv`'s, with its OWN
    `alice@acme.test` -- for pinning that a rate limit computed while
    processing one org's mail can never see another org's flood (invariant
    #5). A same-named `parsed()` keeps call sites in the tests symmetrical
    with `EmailEnv.parsed()`."""

    org_id: UUID
    mailbox: EmailMailbox

    def parsed(self, *, sender: str, message_id: str) -> ParsedMessage:
        return ParsedMessage(
            message_id=message_id,
            from_address=sender,
            subject="Printer trouble",
            recipients=(self.mailbox.address,),
            body_text="",
            attachments=(),
            received_at=datetime.now(UTC),
            refusal=None,
            refusal_reason="",
        )


@dataclass
class EmailEnv:
    """One org, two known senders, a mailbox, and an object store -- the
    shape `tests/test_portal_tickets.py`/`tests/test_service_desk_threads.py`
    already use for the same job, not a second convention."""

    api_env: Any
    engine: AsyncEngine
    admin_engine: AsyncEngine
    org_id: UUID
    mailbox: EmailMailbox
    store: ObjectStore
    alice_id: UUID
    bob_id: UUID
    incident_category_id: UUID
    request_category_id: UUID

    def parsed(
        self,
        *,
        sender: str,
        subject: str = "Printer trouble",
        to: str | None = None,
        body: str = "",
        message_id: str | None = None,
        attachments: tuple[ParsedAttachment, ...] = (),
        refusal: str | None = None,
        refusal_reason: str = "",
    ) -> ParsedMessage:
        return ParsedMessage(
            message_id=message_id or f"<{uuid4().hex}@acme.test>",
            from_address=sender,
            subject=subject,
            recipients=(to or self.mailbox.address,),
            body_text=body,
            attachments=tuple(attachments),
            received_at=datetime.now(UTC),
            refusal=refusal,
            refusal_reason=refusal_reason,
        )

    def _user_for(self, email: str) -> UUID:
        return {"alice@acme.test": self.alice_id, "bob@acme.test": self.bob_id}[email]

    async def a_ticket_for(
        self,
        email: str,
        *,
        ticket_type: str = "incident",
        status: str | None = None,
        closed_at: datetime | None = None,
    ) -> tuple[UUID, str]:
        """A ticket owned by `email`, driven to `status` through the real
        `transitions.change_status` (never a direct status UPDATE, which
        would skip the `resolved_at`/`closed_at` stamping the reopen window
        is measured against), with a real minted `email_token` stored on it."""
        user_id = self._user_for(email)
        category_id = (
            self.incident_category_id if ticket_type == "incident" else self.request_category_id
        )
        token = mint_email_token()
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            ticket = await create_ticket(
                session,
                self.org_id,
                user_id,
                TicketCreate(
                    type="request" if ticket_type == "request" else "incident",
                    category_id=category_id,
                    subject="seeded ticket",
                    description="seeded",
                    requester_id=user_id,
                ),
                origin="email",
            )
            ticket_id = ticket.id
            for to_status in _PATH_TO_STATUS.get((ticket_type, status or ""), ()):
                await change_status(
                    session, self.org_id, ticket_id, to_status=to_status, actor_id=user_id
                )
            ticket.email_token = token

        if closed_at is not None:
            async with AsyncSession(self.admin_engine) as session:
                await session.execute(
                    update(Ticket).where(Ticket.id == ticket_id).values(closed_at=closed_at)
                )
                await session.commit()
        return ticket_id, token

    async def another_org_with_alice(self) -> _OtherOrg:
        """A second org with its own mailbox and its own `alice@acme.test`,
        for the rate-limit cross-org tests. Same seeding shape as the
        `email_env` fixture itself, not a second convention."""
        org_id = await self.api_env.create_org("email-intake-other")
        category_id = await _seed_category(self.api_env, org_id)
        alice_id = await _seed_user(self.api_env, org_id, "alice@acme.test")
        async with org_session(org_id, engine=self.engine, clients=ClientScope.all()) as session:
            mailbox = EmailMailbox(
                org_id=org_id,
                address="helpdesk@acme.test",
                host="imap.acme.test",
                port=993,
                username="helpdesk@acme.test",
                credential_id=await _seed_credential(session, org_id, alice_id),
                default_category_id=category_id,
                created_by=alice_id,
            )
            session.add(mailbox)
            await session.flush()
        return _OtherOrg(org_id=org_id, mailbox=mailbox)

    def stored_object_keys(self) -> list[str]:
        """Every key currently in the object-storage bucket.

        Reaches into `ObjectStore`'s private boto3 client deliberately: the
        assertion this exists for is that a redelivery writes NO new object,
        and only the bucket can answer that. A `ticket_attachment` row count
        cannot -- an orphan is precisely an object with no row pointing at
        it, which is what row-last ordering produced (F2).
        """
        response = self.store._client.list_objects_v2(Bucket=self.store._bucket)
        return sorted(item["Key"] for item in response.get("Contents", ()))

    async def dispose(
        self, *, sender: str = "alice@acme.test", **kwargs: Any
    ) -> InboundMessage:
        """Run one message through `process_message` in its own transaction."""
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return await process_message(
                session,
                self.org_id,
                self.mailbox,
                self.parsed(sender=sender, **kwargs),
                store=self.store,
            )

    async def ticket_for_row(self, row: InboundMessage) -> Ticket:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return (
                await session.execute(select(Ticket).where(Ticket.id == row.ticket_id))
            ).scalar_one()

    async def audit_count(self) -> int:
        from app.models import AuditEvent

        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return len((await session.execute(select(AuditEvent))).scalars().all())

    async def tickets(self) -> list[Ticket]:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return list((await session.execute(select(Ticket))).scalars().all())

    async def ticket_number(self, ticket_id: UUID) -> str:
        async with org_session(
            self.org_id, engine=self.engine, clients=ClientScope.all()
        ) as session:
            return (
                await session.execute(select(Ticket.number).where(Ticket.id == ticket_id))
            ).scalar_one()


@pytest.fixture()
async def email_env(api_env: Any) -> Any:
    with mock_aws():
        store = ObjectStore(api_env.settings)
        org_id = await api_env.create_org("email-intake")
        incident_category_id = await _seed_category(api_env, org_id)
        request_category_id = await _seed_request_category(api_env, org_id)
        alice_id = await _seed_user(api_env, org_id, "alice@acme.test")
        bob_id = await _seed_user(api_env, org_id, "bob@acme.test")

        async with org_session(
            org_id, engine=api_env.app_engine, clients=ClientScope.all()
        ) as session:
            mailbox = EmailMailbox(
                org_id=org_id,
                address="helpdesk@acme.test",
                host="imap.acme.test",
                port=993,
                username="helpdesk@acme.test",
                credential_id=await _seed_credential(session, org_id, alice_id),
                default_category_id=incident_category_id,
                created_by=alice_id,
            )
            session.add(mailbox)
            await session.flush()

        yield EmailEnv(
            api_env=api_env,
            engine=api_env.app_engine,
            admin_engine=api_env.admin_engine,
            org_id=org_id,
            mailbox=mailbox,
            store=store,
            alice_id=alice_id,
            bob_id=bob_id,
            incident_category_id=incident_category_id,
            request_category_id=request_category_id,
        )


def test_reopen_target_covers_every_reopenable_status() -> None:
    """Spec correction C2: `fulfilled -> in_progress` is the request-type
    reopen edge §8's parenthetical omits. Every edge below is one
    `lifecycle.py` actually defines -- assert against `can_transition`, not
    against a hardcoded list, so this cannot drift from the state machine."""
    from app.modules.service_desk.lifecycle import can_transition

    for ticket_type, status, expected in [
        ("incident", "resolved", "open"),
        ("incident", "closed", "open"),
        ("request", "fulfilled", "in_progress"),
        ("request", "closed", "in_progress"),
    ]:
        assert reopen_target(ticket_type, status) == expected
        assert can_transition(ticket_type, status, expected), (ticket_type, status, expected)

    # A live ticket needs no reopen.
    assert reopen_target("incident", "open") is None
    assert reopen_target("request", "in_progress") is None
    # Truly final states are never reopened by an inbound email.
    assert reopen_target("incident", "cancelled") is None
    assert reopen_target("request", "rejected") is None


async def test_a_known_sender_with_no_token_creates_a_ticket(email_env) -> None:
    """Spec §8 row 1."""
    parsed = email_env.parsed(sender="alice@acme.test", subject="Printer broken")
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
        assert row.disposition == "created"
        assert row.sender_user_id == email_env.alice_id

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        ticket = (await session.execute(select(Ticket))).scalar_one()
    assert ticket.origin == "email"
    assert ticket.requester_id == email_env.alice_id
    assert ticket.subject == "Printer broken"
    assert ticket.category_id == email_env.mailbox.default_category_id


async def test_processing_the_same_message_id_twice_creates_one_ticket(email_env) -> None:
    """Spec §11.1 and §4 rule 1. THIS is the idempotency mechanism; it must
    fail if the unique constraint is dropped."""
    parsed = email_env.parsed(sender="alice@acme.test", message_id="<dup@acme.test>")

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )

    with pytest.raises(AlreadyProcessed):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            await process_message(
                session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
            )

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert len((await session.execute(select(Ticket))).scalars().all()) == 1
        assert len((await session.execute(select(InboundMessage))).scalars().all()) == 1


async def test_a_valid_token_from_the_requester_adds_a_public_comment(email_env) -> None:
    """Spec §8 row 2 and §11.8: ALWAYS public. An internal note is not
    expressible from email, by construction."""
    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="still broken"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
        assert row.disposition == "commented"
        assert row.ticket_id == ticket_id

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        comment = (
            await session.execute(select(TicketComment).where(TicketComment.ticket_id == ticket_id))
        ).scalar_one()
    assert comment.visibility == "public"
    assert comment.author_id == email_env.alice_id
    assert "still broken" in comment.body


async def test_a_valid_token_from_the_wrong_sender_is_quarantined(email_env) -> None:
    """Spec §0.2's CENTRAL CLAIM and §11.4. The token is not authority: it
    is in the headers of every message in the thread and outlives the
    requester's employment. Asserted on the OUTCOME, not the parsing.

    Bob's message carries an ATTACHMENT, so this also pins that a rejected
    message writes nothing to object storage -- the quarantine happens before
    any ticket work, and `_attach_all` is never reached.
    """
    from app.models import AuditEvent
    from app.modules.service_desk.models import TicketAttachment

    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        before = (
            await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ).scalar_one()
        victim_status, victim_updated_at = before.status, before.updated_at
    audit_before = await email_env.audit_count()

    parsed = email_env.parsed(
        sender="bob@acme.test",
        to=f"helpdesk+t-{token}@acme.test",
        body="let me in",
        attachments=(ParsedAttachment(filename="payload.png", content=b"\x89PNG\r\n"),),
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "quarantined"
    assert row.sender_user_id == email_env.bob_id
    # The TARGETED ticket is recorded (F6) so an operator investigating
    # "did somebody try to post into this ticket as someone else" can answer
    # it with a query. `ticket_id` here does NOT mean the message landed
    # there -- `disposition` is the authority on that, and the assertions
    # below are what prove it did not.
    assert row.ticket_id == ticket_id
    # ...and the reason stays constant and content-free: it must not carry
    # the victim's subject or body onto a row about someone else's message.
    assert row.reason == "reply token does not belong to this sender"

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        comments = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
    assert comments == [], "bob's mail must not reach alice's ticket"
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert len((await session.execute(select(Ticket))).scalars().all()) == 1, (
            "and it must not become a new ticket either"
        )

    # The victim's ticket is untouched on every observable: not reopened, not
    # even written (a bare `UPDATE ... SET updated_at` would show here).
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        after = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
        attachments = (await session.execute(select(TicketAttachment))).scalars().all()
    assert after.status == victim_status
    assert after.updated_at == victim_updated_at, "the victim's ticket row was written to"
    assert attachments == [], "bob's attachment must not land on alice's ticket"
    assert email_env.stored_object_keys() == [], "nor reach object storage"

    # No audit event either. §9 assigns auditing to the ticket-core services,
    # and none of them ran -- a quarantine is not a privileged action taken on
    # the ticket, it is one refused.
    assert await email_env.audit_count() == audit_before
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        bob_events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.actor_id == email_env.bob_id)
                )
            )
            .scalars()
            .all()
        )
    assert bob_events == []


async def test_an_unknown_sender_is_quarantined_with_no_ticket(email_env) -> None:
    """Spec §11.3."""
    parsed = email_env.parsed(sender="stranger@elsewhere.test")
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "quarantined"
    assert "unknown" in row.reason.lower()
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.parametrize(
    ("ticket_type", "final_status", "expected_status"),
    [
        ("incident", "resolved", "open"),
        ("incident", "closed", "open"),
        ("request", "fulfilled", "in_progress"),
        ("request", "closed", "in_progress"),
    ],
)
async def test_a_reply_within_the_window_reopens_and_comments(
    email_env, ticket_type: str, final_status: str, expected_status: str
) -> None:
    """Spec §11.7 and correction C1. `closed` is in TERMINAL_STATUSES, so
    `add_comment` REJECTS it -- the reopen must happen FIRST. If the
    implementation comments before reopening, the two `closed` cases raise."""
    ticket_id, token = await email_env.a_ticket_for(
        "alice@acme.test", ticket_type=ticket_type, status=final_status
    )
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="it came back"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "reopened"
    assert row.ticket_id == ticket_id

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one()
        comments = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
    assert ticket.status == expected_status
    assert len(comments) == 1, "the reply must land on the ticket, not just reopen it"


async def test_a_reply_beyond_the_window_creates_a_new_ticket_citing_the_old(email_env) -> None:
    """Spec §8 row 4 and §11.7's other direction. Text, not a link row --
    parent/child linking is explicitly NOT sequenced (service-desk §14)."""
    old_id, token = await email_env.a_ticket_for(
        "alice@acme.test",
        status="closed",
        closed_at=datetime.now(UTC) - timedelta(days=400),
    )
    old_number = await email_env.ticket_number(old_id)
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="same thing again"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert row.ticket_id != old_id

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        new_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == row.ticket_id))
        ).scalar_one()
        old_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == old_id))
        ).scalar_one()
    assert old_number in new_ticket.description
    assert old_ticket.status == "closed", "the stale ticket must NOT be resurrected"


async def test_the_window_boundary(email_env, monkeypatch) -> None:
    """Just inside reopens; ON the cutoff reopens; just outside does not.

    THE PROBE IS AT SECONDS SCALE, AND THE CLOCK IS FROZEN, because an
    earlier version of this test probed `window - 1` and `window + 1` DAYS
    and therefore could not detect the off-by-one it exists to catch: both
    `<= timedelta(days=window)` -> `<` and `timedelta(days=window)` ->
    `timedelta(days=window + 1)` survived it (F1). A full day of slack on
    either side of a boundary measured in days tests nothing about the
    boundary.

    Freezing `intake.datetime.now` is what makes the middle probe possible.
    `<=` versus `<` differs on EXACTLY one instant -- the cutoff itself --
    and with a live wall clock that instant is unreachable: any anchor the
    test constructs is already microseconds stale by the time
    `_within_reopen_window` compares it. With the clock pinned, a ticket
    closed at exactly `now - window` sits on the cutoff, and `<=` -> `<`
    turns this test red.
    """
    window = email_env.mailbox.reopen_window_days
    frozen = datetime.now(UTC)
    cutoff = frozen - timedelta(days=window)

    inside_id, inside_token = await email_env.a_ticket_for(
        "alice@acme.test", status="closed", closed_at=cutoff + timedelta(seconds=1)
    )
    exact_id, exact_token = await email_env.a_ticket_for(
        "alice@acme.test", status="closed", closed_at=cutoff
    )
    outside_id, outside_token = await email_env.a_ticket_for(
        "alice@acme.test", status="closed", closed_at=cutoff - timedelta(seconds=1)
    )

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return frozen

    monkeypatch.setattr(intake_module, "datetime", _FrozenClock)

    async def _probe(token: str, tag: str) -> tuple[str, UUID | None]:
        row = await email_env.dispose(
            to=f"helpdesk+t-{token}@acme.test", message_id=f"<{tag}@acme.test>"
        )
        return row.disposition, row.ticket_id

    # One second inside the window: the `+1 day` mutation cannot be seen
    # here, but a wholesale window collapse can.
    assert await _probe(inside_token, "in") == ("reopened", inside_id)
    # EXACTLY on the cutoff: `<=` reopens, `<` does not. This is the probe
    # that pins the comparison operator.
    assert await _probe(exact_token, "exact") == ("reopened", exact_id)
    # One second outside: `timedelta(days=window + 1)` would reopen this.
    outside_disposition, outside_ticket = await _probe(outside_token, "out")
    assert outside_disposition == "created"
    assert outside_ticket != outside_id


async def test_a_refused_loop_message_gets_a_row_and_no_ticket(email_env) -> None:
    """Spec §2.2: EVERY fetched message gets a row. Nothing is silently
    discarded -- a mailbox that quietly eats mail is indistinguishable from
    one nobody is emailing."""
    parsed = email_env.parsed(
        sender="alice@acme.test", refusal="refused_loop", refusal_reason="auto-submitted"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "refused_loop"
    assert row.reason == "auto-submitted"
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


async def test_an_oversized_message_still_delivers_its_text(email_env) -> None:
    """Spec §7: the person is not left silent. Disposition records the
    refusal; the words still reach the ticket."""
    parsed = email_env.parsed(
        sender="alice@acme.test",
        body="the actual words",
        refusal="refused_parse",
        refusal_reason="message exceeds size limit",
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "refused_parse"
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        ticket = (await session.execute(select(Ticket))).scalar_one()
    assert "the actual words" in ticket.description


async def test_attachments_land_on_the_ticket(email_env) -> None:
    from app.modules.email_intake.parsing import ParsedAttachment
    from app.modules.service_desk.models import TicketAttachment

    parsed = email_env.parsed(
        sender="alice@acme.test",
        attachments=(ParsedAttachment(filename="shot.png", content=b"\x89PNG\r\n"),),
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        attachment = (
            await session.execute(
                select(TicketAttachment).where(TicketAttachment.ticket_id == row.ticket_id)
            )
        ).scalar_one()
    assert attachment.filename == "shot.png"
    assert attachment.uploaded_by == email_env.alice_id
    # F10: pin that the attachment went through `threads.add_attachment`,
    # not through a hand-rolled write. The object key is ENTIRELY
    # server-derived (`ObjectStore._ticket_attachment_key`) with no
    # client-supplied segment; an implementation that built its own key --
    # the exact thing `_attach_all`'s docstring forbids -- would pass a
    # filename/uploader assertion but not this one.
    bucket = email_env.api_env.settings.s3_bucket
    assert attachment.object_key == (
        f"s3://{bucket}/{email_env.org_id}/service-desk/tickets/"
        f"{row.ticket_id}/{attachment.id}"
    )


# ---------------------------------------------------------------------------
# Requirements carried in from earlier reviews, absent from the task brief.
# ---------------------------------------------------------------------------


async def test_mail_from_the_orgs_own_notification_sender_is_refused_as_a_loop(
    email_env,
) -> None:
    """Spec §6's other arm: `_detect_loop` in `parsing.py` keeps the
    mailbox's-own-address half, but "the org's own notification sender" needs
    the org's notification-channel config -- a database lookup a pure parser
    cannot do. Without this the platform's own outbound notification becomes
    a comment, which notifies again: the mail loop §6 exists to prevent."""
    from app.models import NotificationChannel

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        session.add(
            NotificationChannel(
                org_id=email_env.org_id,
                kind="email",
                name="outbound",
                config={
                    "host": "smtp.acme.test",
                    "port": 587,
                    "from_address": "no-reply@acme.test",
                },
                created_by=email_env.alice_id,
            )
        )

    # The sender is ALSO a known user, so this cannot pass by accident
    # through the unknown-sender quarantine branch.
    await _seed_user(email_env.api_env, email_env.org_id, "no-reply@acme.test")

    parsed = email_env.parsed(sender="No-Reply@acme.test", body="your ticket was updated")
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "refused_loop"
    assert "notification sender" in row.reason
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


async def test_an_over_long_reason_is_truncated_rather_than_failing_the_insert(
    email_env,
) -> None:
    """`inbound_message.reason` is `String(200)` and the quarantine reason
    embeds the attacker-controlled `From:` address. An unbounded reason turns
    hostile input into an insert error -- i.e. one message stopping a
    mailbox, routed around parsing.py's own no-raise guarantee."""
    long_sender = "x" * 300 + "@elsewhere.test"
    parsed = email_env.parsed(sender=long_sender, subject="s" * 900)
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "quarantined"
    assert len(row.reason) <= 200
    assert len(row.subject) <= 512
    assert len(row.from_address) <= 320


async def test_an_unsanitizable_attachment_filename_does_not_stop_the_message(
    email_env,
) -> None:
    """`app.core.attachments.sanitize_attachment_filename` is ASCII-only, so
    an ordinary non-Latin attachment name ("発票.pdf" in a non-Latin locale)
    is REJECTED. Letting that raise would abort the transaction, leave the
    message unprocessed and stop the mailbox forever on entirely legitimate
    mail. The ticket must still be created; the attachment is dropped and
    said so.

    SAID SO IN THE TICKET TEXT, not only in a counter on the disposition
    row. The row reads `created`, so §10's quarantine-count surfacing misses
    it entirely, and the requester -- who has no reason to think anything was
    lost -- is the one person who can fix it by resending. Naming the file in
    the ticket body reaches them and the technician through the channel they
    are already reading."""
    from app.modules.service_desk.models import TicketAttachment

    parsed = email_env.parsed(
        sender="alice@acme.test",
        body="see attached",
        attachments=(
            ParsedAttachment(filename="発票.pdf", content=b"%PDF-1.4"),
            ParsedAttachment(filename="ok.txt", content=b"fine"),
        ),
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert "attachment" in row.reason.lower()

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        names = (
            (
                await session.execute(
                    select(TicketAttachment.filename).where(
                        TicketAttachment.ticket_id == row.ticket_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert names == ["ok.txt"]

    ticket = await email_env.ticket_for_row(row)
    assert "発票.pdf" in ticket.description, (
        "the requester must be told WHICH file to resend, not just that one was lost"
    )
    assert "see attached" in ticket.description, "the note must not displace their words"


async def test_redelivery_writes_no_further_storage_objects(email_env) -> None:
    """Spec §4 rule 1: the `inbound_message` row is written FIRST.

    This is the assertion that makes the ordering observable. Same-transaction
    atomicity alone does NOT cover it: `threads.add_attachment` performs an
    object-storage PUT before the commit, and object storage has no rollback.
    With the row written LAST, every redelivery re-ran `create_ticket` and the
    PUT and then discarded only the database side -- measured at 4 objects in
    the bucket behind 1 `ticket_attachment` row after three redeliveries, with
    nothing pointing at the orphans and no sweep to collect them. Row-first
    makes the collision happen before any ticket work and any storage write.

    Redelivery is not exotic: §4 is explicit that a crash between the ticket
    write and the `\\Seen` flag is expected, so anything that causes one causes
    unbounded storage growth.
    """
    parsed = email_env.parsed(
        sender="alice@acme.test",
        message_id="<redelivered@acme.test>",
        body="see attached",
        attachments=(ParsedAttachment(filename="shot.png", content=b"\x89PNG\r\n"),),
    )

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"

    after_first_delivery = email_env.stored_object_keys()
    assert len(after_first_delivery) == 1, "the first delivery stores exactly one object"

    for _ in range(3):
        with pytest.raises(AlreadyProcessed):
            async with org_session(
                email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
            ) as session:
                await process_message(
                    session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
                )

    assert email_env.stored_object_keys() == after_first_delivery, (
        "a redelivery must not write another object -- storage is not rolled back"
    )

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        from app.modules.service_desk.models import TicketAttachment

        attachments = (
            (await session.execute(select(TicketAttachment))).scalars().all()
        )
        tickets = (await session.execute(select(Ticket))).scalars().all()
        rows = (await session.execute(select(InboundMessage))).scalars().all()
    assert len(attachments) == 1
    assert len(tickets) == 1
    assert len(rows) == 1


async def test_a_queueless_default_category_is_recorded_not_raised(email_env) -> None:
    """Spec §2.2: EVERY fetched message gets a row.

    A `default_category_id` whose category has no `default_queue_id` is a
    misconfiguration an org-admin can reach today -- and `create_ticket`
    refuses it. Letting that refusal propagate wrote NO row: the poller could
    not mark `\\Seen`, the message was refetched and failed identically every
    interval, and the operator saw nothing at all, because the diagnosis
    surface (`/messages`) was empty. That is precisely the stalled mailbox
    §11.10 forbids, with no path to the cause.

    The row must exist, name the problem, and let the poll continue.
    """
    email_env.mailbox.default_category_id = await _seed_queueless_category(
        email_env.api_env, email_env.org_id
    )

    row = await email_env.dispose(body="printer broken again")

    assert row.disposition == "quarantined"
    assert "queue" in row.reason.lower(), f"the reason must name the problem: {row.reason!r}"
    assert row.ticket_id is None
    assert row.sender_user_id == email_env.alice_id
    assert await email_env.tickets() == []

    # The row is COMMITTED, not just returned -- that is what lets the poller
    # mark the message `\Seen` and stop refetching it.
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        stored = (await session.execute(select(InboundMessage))).scalar_one()
    assert stored.disposition == "quarantined"

    # And the mailbox is not stuck: the next message is processed normally
    # once the category is fixed.
    email_env.mailbox.default_category_id = email_env.incident_category_id
    recovered = await email_env.dispose(body="and again")
    assert recovered.disposition == "created"


async def test_a_mailbox_pointed_at_a_request_category_creates_a_request(email_env) -> None:
    """D3. The new ticket's `type` comes from the CONFIGURED CATEGORY, not a
    hardcoded `"incident"`.

    `create_ticket` rejects a `type`/`category.type` mismatch as a typed
    error, and Task 2 does not constrain `default_category_id` to incident
    categories -- so with the literal, an org that pointed its mailbox at a
    request category would have had every inbound message refused,
    permanently, with nothing in the mailbox form to warn them. Reverting to
    `type="incident"` must turn this red.
    """
    email_env.mailbox.default_category_id = email_env.request_category_id

    row = await email_env.dispose(subject="need a laptop", body="mine died")

    assert row.disposition == "created"
    ticket = await email_env.ticket_for_row(row)
    assert ticket.type == "request"
    assert ticket.category_id == email_env.request_category_id


async def test_an_over_long_subject_is_clamped_to_the_ticket_bound(email_env) -> None:
    """D1. `inbound_message.subject` is `String(512)` but `ticket.subject` is
    `String(255)`, and `TicketCreate.subject` bounds it with a PYDANTIC
    `max_length=255` -- so an unclamped 400-character subject raises
    `ValidationError` out of `create_ticket`. That is one long-subject email
    stopping the mailbox forever, routed around `parse_message`'s no-raise
    guarantee. Removing the clamp must turn this red."""
    row = await email_env.dispose(subject="s" * 400, body="hello")

    assert row.disposition == "created"
    assert len(row.subject) == 400, "the 512-wide message row keeps the full subject"
    ticket = await email_env.ticket_for_row(row)
    assert len(ticket.subject) == 255


async def test_an_over_long_body_is_clamped_to_the_ticket_bound(email_env) -> None:
    """D2, and the worse half: `TicketCreate.description` is a Pydantic
    `max_length=20_000` while `parsing.py` bounds `body_text` at 1 MiB. A
    forwarded thread over 20 000 characters is routine, and unclamped it
    raised on every delivery AND every redelivery, forever. Removing the
    clamp must turn this red."""
    row = await email_env.dispose(body="the actual words " + "x" * 30_000)

    assert row.disposition == "created"
    ticket = await email_env.ticket_for_row(row)
    assert len(ticket.description) == 20_000
    assert ticket.description.startswith("the actual words"), (
        "the clamp must keep the beginning -- what the person actually wrote"
    )


async def test_a_sender_matching_two_users_is_quarantined(email_env) -> None:
    """Spec §8 row 7. Two users in one org may legitimately share an address;
    the module must NEVER guess which human sent it. Covered at the
    `resolve_sender` level in Task 4 -- this pins that it is reachable
    through `process_message` and produces a row and no ticket."""
    await _seed_user(email_env.api_env, email_env.org_id, "alice@acme.test")

    row = await email_env.dispose(body="which of me is this")

    assert row.disposition == "quarantined"
    assert "ambiguous" in row.reason.lower()
    assert row.ticket_id is None
    assert row.sender_user_id is None
    assert await email_env.tickets() == []


async def test_an_empty_body_becomes_a_placeholder(email_env) -> None:
    """D6. Subject-only mail is common (an "it's broken" with nothing in the
    body, or an attachment with no covering text). An empty
    `ticket.description` reads as a rendering bug rather than as what
    happened."""
    row = await email_env.dispose(subject="printer is dead", body="   \n  ")

    assert row.disposition == "created"
    ticket = await email_env.ticket_for_row(row)
    assert ticket.description == "(no message body)"


async def test_an_unrecognised_parser_refusal_fails_closed(email_env) -> None:
    """F7. If `parsing.py` ever emits a refusal value that is not one of the
    seven dispositions, recording the message as `created` would discard the
    refusal and report a damaged message as a clean one -- the one thing
    §2.2 exists to prevent. An unknown refusal is still a refusal."""
    row = await email_env.dispose(
        body="the actual words",
        refusal="refused_something_new",
        refusal_reason="a value this module does not know",
    )

    assert row.disposition == "refused_parse"
    assert "refused_something_new" in row.reason
    # §7's guarantee still holds: the person is not left silent.
    ticket = await email_env.ticket_for_row(row)
    assert "the actual words" in ticket.description


async def test_a_mailbox_from_another_org_is_refused(email_env, api_env) -> None:
    """F8, invariant #5's module-level half. `process_message` takes the
    tenant and the mailbox as INDEPENDENT arguments and then writes
    `mailbox_id` and reads `mailbox.default_category_id` off the mailbox. RLS
    and Task 7's poller make a mismatch unreachable in practice; the defence
    here should be present-and-redundant, not absent."""
    other_org = await api_env.create_org("email-intake-foreign")
    email_env.mailbox.org_id = other_org

    with pytest.raises(ValueError, match="belongs to org"):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test"),
                store=email_env.store,
            )


async def test_a_reply_to_a_cancelled_ticket_becomes_a_new_ticket(email_env) -> None:
    """`cancelled`/`rejected` are in `TERMINAL_STATUSES` (so `add_comment`
    rejects them) and have no reopen edge (so `reopen_target` is None).
    Commenting would raise and stop the mailbox; the requester clearly still
    wants something, so this takes §8 row 4's route: a new ticket citing the
    old number."""
    old_id, token = await email_env.a_ticket_for("alice@acme.test", status="cancelled")
    old_number = await email_env.ticket_number(old_id)
    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="still need this"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert row.ticket_id != old_id

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        new_ticket = (
            await session.execute(select(Ticket).where(Ticket.id == row.ticket_id))
        ).scalar_one()
        old_ticket = (await session.execute(select(Ticket).where(Ticket.id == old_id))).scalar_one()
    assert old_number in new_ticket.description
    assert old_ticket.status == "cancelled"


async def test_an_unresolvable_token_falls_through_to_a_new_ticket(email_env) -> None:
    """A token that resolves to nothing (a ticket deleted, a forged value of
    the right shape) is not an error -- it is the same as no token at all."""
    parsed = email_env.parsed(
        sender="alice@acme.test",
        to=f"helpdesk+t-{mint_email_token()}@acme.test",
        body="hello",
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert row.ticket_id is not None


async def test_a_token_for_another_orgs_ticket_never_resolves(email_env, api_env) -> None:
    """Invariant #5. `resolve_ticket_by_token` is org-scoped; a token minted
    in another org must fall through to new-ticket handling here, never
    comment across the tenant boundary."""
    other_org = await api_env.create_org("email-intake-other")
    other_category = await _seed_category(api_env, other_org)
    other_user = await _seed_user(api_env, other_org, "alice@acme.test")
    token = mint_email_token()
    async with org_session(
        other_org, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        ticket = await create_ticket(
            session,
            other_org,
            other_user,
            TicketCreate(
                type="incident",
                category_id=other_category,
                subject="other org",
                requester_id=other_user,
            ),
            origin="email",
        )
        ticket.email_token = token
        other_ticket_id = ticket.id

    parsed = email_env.parsed(
        sender="alice@acme.test", to=f"helpdesk+t-{token}@acme.test", body="cross-tenant"
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )
    assert row.disposition == "created"
    assert row.ticket_id != other_ticket_id

    async with org_session(
        other_org, engine=api_env.app_engine, clients=ClientScope.all()
    ) as session:
        comments = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == other_ticket_id)
                )
            )
            .scalars()
            .all()
        )
    assert comments == []


# ---------------------------------------------------------------------------
# Task 6: rate limiting (spec §6's second arm).
# ---------------------------------------------------------------------------


async def test_a_flood_from_one_sender_is_refused_rate(email_env) -> None:
    """Spec §6: a legitimate human does not send forty replies a minute; a
    loop does. Every refusal still gets a row -- being rate-limited is a
    thing an operator must be able to see."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    dispositions = []
    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<flood{i}@acme.test>"),
                store=email_env.store,
            )
            dispositions.append(row.disposition)

    assert dispositions[0] == "created"
    assert dispositions[-1] == "refused_rate"
    assert dispositions.count("refused_rate") >= 2


async def test_the_sender_rate_limit_is_scoped_to_one_sender(email_env) -> None:
    """Bob must not be locked out by Alice's flood -- a global limit would
    let one loop deny the whole helpdesk."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<a{i}@acme.test>"),
                store=email_env.store,
            )

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(sender="bob@acme.test", message_id="<b1@acme.test>"),
            store=email_env.store,
        )
    assert row.disposition == "created"


async def test_the_rate_limit_is_scoped_to_one_org(email_env, api_env) -> None:
    """Invariant #5: one org's loop must not throttle another's helpdesk."""
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT

    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(sender="alice@acme.test", message_id=f"<x{i}@acme.test>"),
                store=email_env.store,
            )

    other = await email_env.another_org_with_alice()
    async with org_session(
        other.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, other.org_id, other.mailbox,
            other.parsed(sender="alice@acme.test", message_id="<other1@acme.test>"),
            store=email_env.store,
        )
    assert row.disposition == "created"


async def test_the_sender_rate_limit_org_predicate_is_load_bearing(email_env) -> None:
    """`test_the_rate_limit_is_scoped_to_one_org` above runs entirely under
    `org_session`, where RLS rejects a cross-org row before
    `_sender_rate_limited`'s own `org_id` predicate ever gets a chance to --
    deleting that predicate from the query would leave that test GREEN
    (exactly the "test proves nothing" failure mode this module has hit
    before: a cross-org test that stays green with the `org_id` predicate
    deleted, because RLS rejected first).

    This probe runs on the RLS-BYPASSING admin engine on purpose, mirroring
    `test_email_intake_identity.py::test_the_sender_org_predicate_is_load_bearing`,
    so `_sender_rate_limited`'s own `org_id` filter -- not RLS -- is the only
    thing that can save it.
    """
    from app.modules.email_intake.intake import SENDER_RATE_LIMIT, _sender_rate_limited

    other = await email_env.another_org_with_alice()
    for i in range(SENDER_RATE_LIMIT + 2):
        async with org_session(
            other.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            await process_message(
                session,
                other.org_id,
                other.mailbox,
                other.parsed(sender="alice@acme.test", message_id=f"<o{i}@acme.test>"),
                store=email_env.store,
            )

    # `email_env`'s own org has sent no mail from alice at all -- if the
    # count below is scoped correctly, it must see zero of the other org's
    # flood and report "not rate-limited".
    async with admin_session(engine=email_env.admin_engine) as session:
        reason = await _sender_rate_limited(session, email_env.org_id, "alice@acme.test")
    assert reason is None, (
        f"CROSS-ORG LEAK: email_env.org_id's alice was rate-limited by another org's "
        f"flood: {reason!r}"
    )


async def test_a_flood_of_replies_to_one_ticket_is_refused_rate(email_env) -> None:
    """Spec §6's second limit: the per-ticket inbound comment cap.

    Exercised where the per-SENDER limit (10/minute) cannot mask it. Every
    legitimate reply to one ticket comes from that ticket's own requester
    (§0.2's central claim), so a naive flood-of-replies-to-one-ticket test
    trips the SENDER limit at message 11 and never actually exercises
    TICKET_RATE_LIMIT (20) at all -- the disposition still reads
    `refused_rate` either way, so a version of this test that just floods
    the ticket as fast as possible would stay green even with the
    per-ticket check deleted entirely. Each row's `created_at` is pushed
    outside `SENDER_RATE_WINDOW` (but kept inside `TICKET_RATE_WINDOW`)
    immediately after it lands, simulating a slow-but-steady trickle that
    never trips the per-sender limit, so the per-ticket count is the only
    thing that can be responsible for the eventual refusal.
    """
    from app.modules.email_intake.intake import (
        SENDER_RATE_WINDOW,
        TICKET_RATE_LIMIT,
        TICKET_RATE_WINDOW,
    )

    assert SENDER_RATE_WINDOW * 3 < TICKET_RATE_WINDOW, (
        "the backdating below assumes the sender window is well inside the ticket window"
    )

    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")
    dispositions = []
    last_message_id = ""
    for i in range(TICKET_RATE_LIMIT + 2):
        last_message_id = f"<reply{i}@acme.test>"
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(
                    sender="alice@acme.test",
                    to=f"helpdesk+t-{token}@acme.test",
                    message_id=last_message_id,
                ),
                store=email_env.store,
            )
            dispositions.append(row.disposition)
        # Push this row's `created_at` outside the SENDER window (but still
        # inside the TICKET window) right away, so the *next* iteration's
        # sender-level count cannot see it. Without this, alice's own sender
        # limit trips at message 11 and this test would pass even with the
        # per-ticket check deleted -- see the docstring above.
        async with AsyncSession(email_env.admin_engine) as admin:
            await admin.execute(
                update(InboundMessage)
                .where(InboundMessage.id == row.id)
                .values(created_at=datetime.now(UTC) - SENDER_RATE_WINDOW * 2)
            )
            await admin.commit()

    assert dispositions[:TICKET_RATE_LIMIT].count("refused_rate") == 0, (
        "no reply should be refused before TICKET_RATE_LIMIT replies have landed -- "
        f"got {dispositions}"
    )
    assert dispositions[-1] == "refused_rate"
    assert dispositions.count("refused_rate") >= 2

    # And it is genuinely the TICKET that is rate-limited, not the sender:
    # the row names the targeted ticket, same convention as the wrong-sender
    # quarantine case.
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        last_row = (
            await session.execute(
                select(InboundMessage).where(InboundMessage.message_id == last_message_id)
            )
        ).scalar_one()
    assert last_row.ticket_id == ticket_id
    assert "ticket" in last_row.reason.lower()


async def _backdate_out_of_the_sender_window(email_env: Any, row_id: UUID) -> None:
    """Push one `inbound_message` row's `created_at` outside
    `SENDER_RATE_WINDOW` but well inside `TICKET_RATE_WINDOW`.

    The same device `test_a_flood_of_replies_to_one_ticket_is_refused_rate`
    uses, and for the same reason: without it the per-SENDER limit trips at
    message 11 and masks whatever the per-TICKET count is doing.
    """
    from app.modules.email_intake.intake import SENDER_RATE_WINDOW

    async with AsyncSession(email_env.admin_engine) as admin:
        await admin.execute(
            update(InboundMessage)
            .where(InboundMessage.id == row_id)
            .values(created_at=datetime.now(UTC) - SENDER_RATE_WINDOW * 2)
        )
        await admin.commit()


async def test_quarantined_wrong_sender_mail_does_not_rate_limit_the_requester(
    email_env,
) -> None:
    """A THIRD PARTY MUST NOT BE ABLE TO SILENCE A REQUESTER ON THEIR OWN
    TICKET.

    The seam this pins is between two independently-correct fixes: the
    wrong-sender quarantine records the TARGETED `ticket_id` (deliberately,
    so an operator can query "did somebody try to post into this ticket as
    someone else"), and the per-ticket rate limit counts rows by
    `ticket_id`. Counting the quarantines made possession of the token --
    which travels with every forward of the thread (§0.2) -- sufficient to
    lock the ticket's own requester out of replying by email: every message
    is correctly refused, and the refusal itself consumes the victim's
    budget. No comment, no bounce, and self-reinforcing for as long as the
    flood continues.

    Bob holds alice's token and floods it. Every one of his messages is
    correctly quarantined and touches nothing. Alice's own next reply must
    still become a public comment on her ticket.
    """
    from app.modules.email_intake.intake import TICKET_RATE_LIMIT

    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")

    bob_dispositions = []
    for i in range(TICKET_RATE_LIMIT + 4):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(
                    sender="bob@acme.test",
                    to=f"helpdesk+t-{token}@acme.test",
                    message_id=f"<insider{i}@acme.test>",
                ),
                store=email_env.store,
            )
            bob_dispositions.append(row.disposition)
        # Keep bob under HIS OWN per-sender limit, so that every one of his
        # messages really is a quarantine and the only thing that could
        # refuse alice below is the per-TICKET count.
        await _backdate_out_of_the_sender_window(email_env, row.id)

    assert set(bob_dispositions) == {"quarantined"}, (
        f"the flood must be quarantined, not refused for some other reason: {bob_dispositions}"
    )

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(
                sender="alice@acme.test",
                to=f"helpdesk+t-{token}@acme.test",
                body="here is the serial number you asked for",
                message_id="<alice-genuine@acme.test>",
            ),
            store=email_env.store,
        )
    assert row.disposition == "commented", (
        "the requester was locked out of her own ticket by mail that never "
        f"reached it: {row.disposition} ({row.reason!r})"
    )
    assert row.ticket_id == ticket_id

    # ...and it really landed, on STORED STATE, not merely on the row.
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        comments = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
    assert [c.body for c in comments] == ["here is the serial number you asked for"]


async def test_genuine_replies_still_reach_the_per_ticket_limit(email_env) -> None:
    """The other half of the pair above: narrowing the per-ticket count to
    the dispositions that actually landed must not amount to switching the
    limit off.

    Alice's own replies -- recorded `commented`, the disposition the count
    keeps -- still fill the ticket's budget, and interleaved wrong-sender
    quarantines neither add to it nor subtract from it. Backdated out of the
    per-SENDER window throughout, so the refusal at the end can only be the
    per-TICKET count.
    """
    from app.modules.email_intake.intake import TICKET_RATE_LIMIT

    ticket_id, token = await email_env.a_ticket_for("alice@acme.test")

    dispositions = []
    for i in range(TICKET_RATE_LIMIT):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(
                    sender="alice@acme.test",
                    to=f"helpdesk+t-{token}@acme.test",
                    message_id=f"<genuine{i}@acme.test>",
                ),
                store=email_env.store,
            )
            dispositions.append(row.disposition)
        await _backdate_out_of_the_sender_window(email_env, row.id)

    assert dispositions == ["commented"] * TICKET_RATE_LIMIT, (
        f"every reply up to the limit must land: {dispositions}"
    )

    # A few quarantines mixed in; they must not change the answer either way.
    for i in range(3):
        async with org_session(
            email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
        ) as session:
            row = await process_message(
                session,
                email_env.org_id,
                email_env.mailbox,
                email_env.parsed(
                    sender="bob@acme.test",
                    to=f"helpdesk+t-{token}@acme.test",
                    message_id=f"<mixed{i}@acme.test>",
                ),
                store=email_env.store,
            )
        assert row.disposition == "quarantined"
        await _backdate_out_of_the_sender_window(email_env, row.id)

    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session,
            email_env.org_id,
            email_env.mailbox,
            email_env.parsed(
                sender="alice@acme.test",
                to=f"helpdesk+t-{token}@acme.test",
                body="and another thing",
                message_id="<genuine-over@acme.test>",
            ),
            store=email_env.store,
        )
    assert row.disposition == "refused_rate", (
        f"the per-ticket limit no longer refuses anything: {row.disposition}"
    )
    assert row.ticket_id == ticket_id

    # The refusal is real: the ticket did NOT receive the comment.
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        comments = (
            (
                await session.execute(
                    select(TicketComment).where(TicketComment.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(comments) == TICKET_RATE_LIMIT
    assert all("and another thing" not in c.body for c in comments)


async def test_the_ticket_rate_limit_org_predicate_is_load_bearing(email_env) -> None:
    """F2. `_ticket_rate_limited` pairs `org_id` with `ticket_id` (invariant
    #5 as a standing discipline), but every test that reaches it runs under
    `org_session`, where RLS rejects a cross-org row first -- so deleting the
    predicate leaves them all green.

    This probe runs on the RLS-BYPASSING admin engine, exactly as
    `test_the_sender_rate_limit_org_predicate_is_load_bearing` and
    `test_email_intake_identity.py::test_the_ticket_org_predicate_is_load_bearing`
    do, so the function's own `org_id` filter is the only thing that can
    save it: another org's ticket is over the limit, and `email_env`'s org
    must see none of it.
    """
    from app.models import User
    from app.modules.email_intake.intake import TICKET_RATE_LIMIT, _ticket_rate_limited
    from app.modules.service_desk.models import TicketCategory

    other = await email_env.another_org_with_alice()
    async with org_session(
        other.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        other_alice = (
            await session.execute(
                select(User.id).where(User.email == "alice@acme.test")
            )
        ).scalar_one()
        other_category = (
            await session.execute(select(TicketCategory.id))
        ).scalars().first()
        assert other_category is not None
        other_ticket = await create_ticket(
            session,
            other.org_id,
            other_alice,
            TicketCreate(
                type="incident",
                category_id=other_category,
                subject="other org ticket",
                requester_id=other_alice,
            ),
            origin="email",
        )
        other_ticket_id = other_ticket.id

    # Put that ticket well over the limit, with the dispositions the count
    # keeps. Written directly because what is under test is the COUNT's
    # scoping, not how the rows came to exist.
    async with AsyncSession(email_env.admin_engine) as admin:
        for i in range(TICKET_RATE_LIMIT + 5):
            admin.add(
                InboundMessage(
                    org_id=other.org_id,
                    mailbox_id=other.mailbox.id,
                    message_id=f"<xorg{i}@acme.test>",
                    from_address="alice@acme.test",
                    subject="reply",
                    received_at=datetime.now(UTC),
                    disposition="commented",
                    reason="",
                    ticket_id=other_ticket_id,
                    sender_user_id=other_alice,
                )
            )
        await admin.commit()

    async with admin_session(engine=email_env.admin_engine) as session:
        reason = await _ticket_rate_limited(session, email_env.org_id, other_ticket_id)
    assert reason is None, (
        "CROSS-ORG LEAK: another org's flooded ticket rate-limited this org: "
        f"{reason!r}"
    )


async def test_a_message_that_fails_to_parse_is_not_counted_as_quarantined(
    email_env,
) -> None:
    """F3. `parse_message` returns `refused_parse` for mail with no `From:`,
    an unshaped `From:`, or a broken MIME structure -- and such a message
    has, by construction, no resolvable sender, so it used to fall through
    to the unknown-sender branch, be filed `quarantined`, and lose the
    parser's reason.

    `quarantined_count` is the alarm spec §10 designates for "mail a human
    expected to become a ticket and which did not"; against a published
    helpdesk address, malformed spam landing in it makes the number mean
    nothing. The parse failure is recorded AS a parse failure, with the
    parser's reason intact.
    """
    parsed = email_env.parsed(
        sender="not-an-address",
        refusal="refused_parse",
        refusal_reason="malformed MIME structure",
    )
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        row = await process_message(
            session, email_env.org_id, email_env.mailbox, parsed, store=email_env.store
        )

    assert row.disposition == "refused_parse", (
        f"a parse failure inflates §10's quarantine alarm: {row.disposition}"
    )
    assert row.reason == "malformed MIME structure", (
        f"the parser's reason was discarded: {row.reason!r}"
    )
    # Still no ticket, and still no sender attributed to it.
    assert row.ticket_id is None
    assert row.sender_user_id is None
    async with org_session(
        email_env.org_id, engine=email_env.engine, clients=ClientScope.all()
    ) as session:
        assert (await session.execute(select(Ticket))).scalars().all() == []


async def test_an_unknown_sender_that_parsed_fine_is_still_quarantined(email_env) -> None:
    """F3's other side: the narrowing above must not swallow the case §11.3
    is about. A perfectly well-formed message from someone who is not a user
    of this org IS mail a human expected to become a ticket and which did
    not, so it stays `quarantined` and keeps counting toward the alarm."""
    row = await email_env.dispose(sender="stranger@elsewhere.test")
    assert row.disposition == "quarantined"
    assert "unknown" in row.reason.lower()
